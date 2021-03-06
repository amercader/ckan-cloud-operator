#### standard provider code ####

# import the correct PROVIDER_SUBMODULE and PROVIDER_ID constants for your provider
from .constants import PROVIDER_ID
from ..constants import PROVIDER_SUBMODULE

# define common provider functions based on the constants
from ckan_cloud_operator.providers import manager as providers_manager
def _get_resource_name(suffix=None): return providers_manager.get_resource_name(PROVIDER_SUBMODULE, PROVIDER_ID, suffix=suffix)
def _get_resource_labels(for_deployment=False): return providers_manager.get_resource_labels(PROVIDER_SUBMODULE, PROVIDER_ID, for_deployment=for_deployment)
def _get_resource_annotations(suffix=None): return providers_manager.get_resource_annotations(PROVIDER_SUBMODULE, PROVIDER_ID, suffix=suffix)
def _set_provider(): providers_manager.set_provider(PROVIDER_SUBMODULE, PROVIDER_ID)
def _config_set(key=None, value=None, values=None, namespace=None, is_secret=False, suffix=None): providers_manager.config_set(PROVIDER_SUBMODULE, PROVIDER_ID, key=key, value=value, values=values, namespace=namespace, is_secret=is_secret, suffix=suffix)
def _config_get(key=None, default=None, required=False, namespace=None, is_secret=False, suffix=None): return providers_manager.config_get(PROVIDER_SUBMODULE, PROVIDER_ID, key=key, default=default, required=required, namespace=namespace, is_secret=is_secret, suffix=suffix)
def _config_interactive_set(default_values, namespace=None, is_secret=False, suffix=None, from_file=False): providers_manager.config_interactive_set(PROVIDER_SUBMODULE, PROVIDER_ID, default_values, namespace, is_secret, suffix, from_file)

################################
# custom provider code starts here
#

import datetime
import os
import yaml
import subprocess

from ckan_cloud_operator.infra import CkanInfra
from ckan_cloud_operator import logs
from ckan_cloud_operator.providers.cluster import manager as cluster_manager
from ckan_cloud_operator.drivers.gcloud import driver as gcloud_driver


def initialize(db_prefix=None, interactive=False):
    _set_provider()
    ckan_infra = CkanInfra(required=False)
    default_values = {
        'host': ckan_infra.POSTGRES_HOST,
        'port': '5432',
        'is-private-ip': True,
        'admin-user': ckan_infra.POSTGRES_USER,
        'admin-password': ckan_infra.POSTGRES_PASSWORD,
        'gcloud-sql-instance-name': ckan_infra.GCLOUD_SQL_INSTANCE_NAME,
    }
    if interactive:
        print("\n"
              "Starting interactive initialization of the gcloudsql db provider\n"
              "Please prepare the following values:\n"
              "\n"
              " - admin db host and credentials\n"
              " - gcloudsql instance name and project details\n"
              "\n")
        _config_interactive_set(default_values, **_get_config_credentials_kwargs(db_prefix))
    config = _config_get(**_get_config_credentials_kwargs(db_prefix))
    for key, default_value in default_values.items():
        if key not in config and default_value:
            _config_set(key, default_value, **_get_config_credentials_kwargs(db_prefix))
        elif not config.get(key):
            raise Exception(f'missing key: {key}')


def get_postgres_internal_host_port(db_prefix=None):
    host = _credentials_get(db_prefix, key='host', required=True)
    port = int(_credentials_get(db_prefix, key='port', required=True))
    return host, port


def get_postgres_external_host_port(db_prefix=None):
    assert _credentials_get(db_prefix, key='is-private-ip', required=False) != 'y', 'direct access to the DB is not supported, please enable the db proxy'
    host, port = get_postgres_internal_host_port(db_prefix)
    return host, port


def get_postgres_admin_credentials(db_prefix=None):
    credentials = _credentials_get(db_prefix)
    return credentials['admin-user'], credentials['admin-password'], credentials.get('admin-db-name', credentials['admin-user'])


def is_private_ip(db_prefix=None):
    return _credentials_get(db_prefix, key='is-private-ip', required=False) == 'y'


def import_db(import_url, target_db_name, import_user, db_prefix=None):
    gcloud_driver._import_gcloud_sql_db(
        *_gcloud().get_project_zone(),
        _sql_instance_name(db_prefix),
        import_url, target_db_name, import_user
    )


def create_backup(database, connection_string=None, db_prefix=None):
    filename = f'{database}_' + datetime.datetime.now().strftime('%Y%m%d%H%M') + '.gz'
    gs_url = os.path.join(
        _credentials_get(db_prefix, key='backups-gs-base-url', required=True),
        datetime.datetime.now().strftime('%Y/%m/%d/%H'),
        filename
    )
    if not connection_string:
        from ckan_cloud_operator.providers.db import manager as db_manager
        connection_string = db_manager.get_external_admin_connection_string(db_name=database)
    logs.info(f'Dumping DB: {filename}')
    subprocess.check_call([
        "bash", "-o", "pipefail", "-c",
            f"pg_dump -d {connection_string} --format=plain --no-owner --no-acl --schema=public | "
            f"sed -E 's/(DROP|CREATE|COMMENT ON) EXTENSION/-- \\1 EXTENSION/g' | "
            f"gzip -c > {filename}",
    ])
    subprocess.check_call(f'ls -lah {filename}', shell=True)
    logs.info(f'Copying to: {gs_url}')
    gcloud_driver.check_call(
        *_gcloud().get_project_zone(),
        f'cp -m ./{filename} {gs_url} && rm {filename}',
        gsutil=True
    )


def create_all_backups():
    logs.info('Fetching all database names')
    from ckan_cloud_operator.providers.db import manager as db_manager
    db_names = [db[0] for db in db_manager.get_all_dbs_users()[0] if db[0] != 'postgres']
    logs.info('{} DBs'.format(len(db_names)))
    [create_backup(db) for db in db_names]


def get_operation_status(operation_id):
    return yaml.load(gcloud_driver.check_output(
        *_gcloud().get_project_zone(),
        f'sql operations describe {operation_id}'
    ).decode())


def _credentials_get(db_prefix, key=None, default=None, required=False):
    return _config_get(key=key, default=default, required=required, **_get_config_credentials_kwargs(db_prefix))


def _get_config_credentials_kwargs(db_prefix):
    return {
        'is_secret': True,
        'suffix': f'{db_prefix}-credentials' if db_prefix else 'credentials'
    }


def _gcloud():
    return cluster_manager.get_provider()


def _sql_instance_name(db_prefix):
    return _credentials_get(db_prefix, key='gcloud-sql-instance-name')
