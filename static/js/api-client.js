(function () {
    "use strict";

    function ApiError(message, code, status) {
        this.name = "ApiError";
        this.message = message;
        this.code = code || "request_failed";
        this.status = status || 0;
        if (Error.captureStackTrace) {
            Error.captureStackTrace(this, ApiError);
        }
    }
    ApiError.prototype = Object.create(Error.prototype);
    ApiError.prototype.constructor = ApiError;

    function ApiClient() {
        this.token = window.sessionStorage.getItem("jarvis.apiToken") || "";
    }

    ApiClient.prototype.setToken = function (token) {
        this.token = String(token || "").trim();
        if (this.token) {
            window.sessionStorage.setItem("jarvis.apiToken", this.token);
        } else {
            window.sessionStorage.removeItem("jarvis.apiToken");
        }
    };

    ApiClient.prototype.request = async function (path, options) {
        options = options || {};
        var controller = new AbortController();
        var timeout = window.setTimeout(function () {
            controller.abort();
        }, options.timeout || 75000);
        var headers = new Headers(options.headers || {});
        headers.set("Accept", "application/json");
        if (this.token) {
            headers.set("X-Jarvis-Token", this.token);
        }
        if (options.body && !(options.body instanceof FormData)) {
            headers.set("Content-Type", "application/json");
        }

        try {
            var response = await window.fetch(path, {
                method: options.method || "GET",
                headers: headers,
                body: options.body ? JSON.stringify(options.body) : undefined,
                signal: options.signal || controller.signal,
                credentials: "same-origin"
            });
            var payload;
            try {
                payload = await response.json();
            } catch (_parseError) {
                throw new ApiError("服务返回了无效响应", "invalid_response", response.status);
            }
            if (!response.ok || !payload.ok) {
                var error = payload.error || {};
                if (response.status === 401 && error.code === "unauthorized") {
                    window.dispatchEvent(new CustomEvent("jarvis:auth-required"));
                }
                throw new ApiError(
                    error.message || "请求失败",
                    error.code,
                    response.status
                );
            }
            return payload.data || {};
        } catch (error) {
            if (error.name === "AbortError") {
                throw new ApiError("请求超时，请稍后重试", "timeout", 408);
            }
            throw error;
        } finally {
            window.clearTimeout(timeout);
        }
    };

    window.JarvisApiClient = ApiClient;
    window.JarvisApiError = ApiError;
}());
