/* config.js — site-wide configuration constants.
   Loaded before all other JS so SPARK_CONFIG is available globally. */

window.SPARK_CONFIG = Object.freeze({
  API_BASE: 'https://spark-api.wedd.au/api/v1/public',
  API_ROOT: 'https://spark-api.wedd.au/api/v1',
  FALLBACK_LOCAL: '/data',
  FALLBACK_GITHUB: 'https://raw.githubusercontent.com/adrianwedd/spark/master/site/data'
});
