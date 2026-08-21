window.AnimaxAPI = (function() {
    const config = {
        BASE_URL: '/api',
        RESOLVER_BASE_URL: ''
    };

    const TOKEN_KEY = 'aFttaierVg4u41qWuGVsg7y7DTlh93RFTLri3whPGyfWOaT33VJa';

    // Endpoints classified to route directly to the HLS proxy / streaming microservice
    const RESOLVER_ENDPOINTS = [
        '/streams',
        '/sports',
        '/cdnlivetv',
        '/tunnel',
        '/register',
        '/proxy',
        '/fetch',
        '/api/stream/metadata',
        '/player'
    ];

    function getToken() { return localStorage.getItem(TOKEN_KEY); }
    function setToken(token) { localStorage.setItem(TOKEN_KEY, token); }
    function clearToken() { localStorage.removeItem(TOKEN_KEY); }

    /**
     * Resolves the target base path depending on the pattern of the endpoint.
     */
    function getTargetUrl(endpoint) {
        const isResolver = RESOLVER_ENDPOINTS.some(prefix => endpoint.startsWith(prefix));
        if (isResolver) {
            return `${config.RESOLVER_BASE_URL}${endpoint}`;
        }
        return `${config.BASE_URL}${endpoint}`;
    }

    async function request(endpoint, options = {}) {
        const isResolver = RESOLVER_ENDPOINTS.some(prefix => endpoint.startsWith(prefix));
        const url = getTargetUrl(endpoint);
        
        const headers = {
            'Content-Type': 'application/json',
            ...options.headers
        };

        // Inject authorization only for core backend requests.
        // Bypassing Authorization headers on the resolver microservice mitigates unnecessary CORS preflight complications.
        const token = getToken();
        if (token && !isResolver) {
            headers['Authorization'] = `Bearer ${token}`;
        }

        try {
            const response = await fetch(url, { ...options, headers });
            
            if (response.status === 401) {
                clearToken();
                // Route message up to the shell to trigger Auth Cover
                if (window.parent !== window) {
                    window.parent.postMessage({ type: 'REQUIRE_AUTH' }, '*');
                }
                throw new Error("Unauthorized");
            }

            const data = await response.json();
            if (!response.ok) {
                // Force-log the raw error object so it can be inspected in the DevTools console
                console.warn("[Animax API Debug] Raw Validation Error Data:", data);

                let errorMsg = 'API Error';
                if (data && data.detail) {
                    if (Array.isArray(data.detail)) {
                        errorMsg = data.detail.map(err => {
                            const field = err.loc ? err.loc.slice(1).join('.') : '';
                            return field ? `${field}: ${err.msg}` : err.msg;
                        }).join(', ');
                    } else if (typeof data.detail === 'object') {
                        errorMsg = data.detail.message || JSON.stringify(data.detail);
                    } else {
                        errorMsg = data.detail;
                    }
                }
                throw new Error(errorMsg);
            }
            
            return data;
        } catch (error) {
            console.error(`[Animax API] Error in request to ${endpoint}:`, error);
            throw error;
        }
    }

    return {
        get: (endpoint, options) => request(endpoint, { method: 'GET', ...options }),
        post: (endpoint, body, options) => request(endpoint, { method: 'POST', body: JSON.stringify(body), ...options }),
        getToken, setToken, clearToken,
        config
    };
})();