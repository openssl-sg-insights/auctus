local auctus = import 'auctus.libsonnet';
local ckan = import 'discovery/ckan.libsonnet';
local socrata = import 'discovery/socrata.libsonnet';
local test_discoverer = import 'discovery/test.libsonnet';
local uaz_indicators = import 'discovery/uaz-indicators.libsonnet';
local worldbank = import 'discovery/worldbank.libsonnet';
local zenodo = import 'discovery/zenodo.libsonnet';
local elasticsearch = import 'elasticsearch.libsonnet';
local ingress = import 'ingress.libsonnet';
local jaeger = import 'jaeger.libsonnet';
local minio = import 'minio.libsonnet';
local monitoring = import 'monitoring.libsonnet';
local nominatim = import 'nominatim.libsonnet';
local rabbitmq = import 'rabbitmq.libsonnet';
local redis = import 'redis.libsonnet';
local snapshotter = import 'snapshotter.libsonnet';
local volumes_local = import 'volumes-local.libsonnet';
local volumes = import 'volumes.libsonnet';

local config = {
  image: 'auctus:latest',
  frontend_image: 'auctus_frontend:latest',
  app_domain: 'localhost',
  //frontend_url: 'https://%s' % self.app_domain, // If using Ingress
  frontend_url: 'http://localhost:30808',  // If using KinD
  //api_url: 'https://%s/api/v1' % self.app_domain, // If using Ingress
  api_url: 'http://localhost:30808/api/v1',  // If using KinD
  elasticsearch_prefix: 'auctusdev_',
  nominatim_url: 'http://nominatim:8080/',
  object_store: {
    s3_url: 'http://minio:9000',
    s3_client_url: 'http://files.localhost:30808',
    s3_bucket_prefix: 'auctus-dev-',
    //gcs_project: 'auctus',
    //gcs_bucket_prefix: 'auctus-dev-',
  },
  //smtp: {
  //  host: 'mail.example.org',
  //  from_name: 'Auctus',
  //  from_address: 'auctus@example.org',
  //},
  custom_fields: {},
  //custom_fields: {
  //  specialId: { label: 'Special ID', type: 'integer' },
  //  dept: { label: 'Department', type: 'keyword', required: true },
  //},
  // Addresses to exclude from SSRF protection
  request_whitelist: ['test-discoverer'],
  log_format: 'json',
  // Storage class for volumes (except cache)
  storage_class: 'standard',
  // Node selector for nodes where the cache volume is available
  local_cache_node_selector: [
    //{ key: 'kubernetes.io/os', operator: 'In', values: ['linux'] },
    { key: 'auctus-prod-cache-volume', operator: 'Exists' },
  ],
  // Label on nodes where databases will be run (can be set to null)
  db_node_label: {
    default: null,
    redis: self.default,
    elasticsearch: self.default,
    rabbitmq: self.default,
    minio: self.default,
    lazo: self.default,
    prometheus: self.default,
    grafana: self.default,
    jaeger: self.default,
    nominatim: self.default,
  },
  // Public domain for the coordinator (can be set to null to disable Ingress)
  coordinator_domain: 'coordinator.auctus.vida-nyu.org',
  // Whether Grafana can be access read-only by the public
  grafana_anonymous_access: true,
  // Public domain for Grafana (can be set to null to disable Ingress)
  grafana_domain: 'grafana.auctus.vida-nyu.org',
  // OpenTelemetry configuration (can be null)
  //opentelemetry: null,
  opentelemetry: {
    OTEL_TRACES_EXPORTER: 'jaeger_thrift',
    OTEL_EXPORTER_JAEGER_AGENT_HOST: 'jaeger',
    OTEL_EXPORTER_JAEGER_AGENT_PORT: '6831',
  },
  // Protect the frontend and API with a password
  // If true, the corresponding secret has to be set
  private_app: false,
  // Wrapper for Kubernetes objects
  kube: function(version, kind, payload) (
    std.mergePatch(
      {
        apiVersion: version,
        kind: kind,
      },
      payload,
    )
  ),
};

local files = {
  'volumes.yml': volumes(
    config,
    cache_size='55Gi',
    local_cache_path='/var/lib/auctus/prod/cache',
  ),
  //'volumes.yml': volumes_local(config),
  'redis.yml': redis(
    config,
    maxmemory='500mb',
  ),
  'elasticsearch.yml': elasticsearch(
    config,
    replicas=1,
    heap_size='2g',
  ),
  'rabbitmq.yml': rabbitmq(config),
  'nominatim.yml': nominatim(
    config,
    data_url='https://www.googleapis.com/download/storage/v1/b/nominatim-data-nyu/o/nominatim-postgres-data.tar?alt=media',
  ),
  'auctus.yml': (
    auctus.lazo(config, lazo_memory=2000000000)  // 2 GB
    + auctus.frontend(config)
    + auctus.apiserver(config)
    + auctus.coordinator(config)
    + auctus.cache_cleaner(
      config,
      cache_max_bytes=50000000000,  // 50 GB
    )
    + auctus.profiler(config)
  ),
  'snapshotter.yml': snapshotter(config),
  'ingress.yml': ingress(config),
  'minio.yml': minio(config),
  'monitoring.yml': monitoring(config),
  'jaeger.yml': jaeger(config),
  'discovery/ckan.yml': ckan(
    config,
    domains=['data.humdata.org'],
  ),
  'discovery/socrata.yml': socrata(
    config,
    domains=['data.cityofnewyork.us', 'finances.worldbank.org'],
  ),
  'discovery/uaz-indicators.yml': uaz_indicators(config),
  'discovery/worldbank.yml': worldbank(config),
  'discovery/zenodo.yml': zenodo(
    config,
    keyword_query='covid',
  ),
  //'discovery/test-discoverer.yml': test_discoverer(config),
};

{
  [k]: std.manifestYamlStream(files[k])
  for k in std.objectFields(files)
}
