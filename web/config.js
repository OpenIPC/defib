/**
 * Deploy-time configuration for the OpenIPC CORS proxy.
 *
 * This committed copy is the local-development default: no HMAC key, which is
 * fine because the worker skips HMAC for localhost origins. The GitHub Pages
 * workflow overwrites this file and injects the real key from the
 * CORS_PROXY_HMAC_KEY repository secret, so the key never enters the repo.
 *
 * With no key the proxy is still attempted unsigned — that works from
 * localhost and fails fast (HTTP 400) elsewhere, falling through to the
 * remaining sources.
 */
const CORS_PROXY_URL = 'https://cors-proxy.joseph-nef.workers.dev';
const CORS_PROXY_HMAC_KEY = '';
