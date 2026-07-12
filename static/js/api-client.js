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
        var timedOut = false;
        var timeout = window.setTimeout(function () {
            timedOut = true;
            controller.abort();
        }, options.timeout || 75000);
        var externalSignal = options.signal || null;
        var abortFromExternal = function () { controller.abort(); };
        if (externalSignal) {
            if (externalSignal.aborted) {
                controller.abort();
            } else {
                externalSignal.addEventListener("abort", abortFromExternal, { once: true });
            }
        }
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
                signal: controller.signal,
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
                throw new ApiError(
                    timedOut ? "请求超时，请稍后重试" : "请求已取消",
                    timedOut ? "timeout" : "cancelled",
                    timedOut ? 408 : 0
                );
            }
            throw error;
        } finally {
            window.clearTimeout(timeout);
            if (externalSignal) {
                externalSignal.removeEventListener("abort", abortFromExternal);
            }
        }
    };

    ApiClient.prototype.stream = async function (path, options, onEvent) {
        options = options || {};
        var controller = new AbortController();
        var timedOut = false;
        var timeout = window.setTimeout(function () {
            timedOut = true;
            controller.abort();
        }, options.timeout || 610000);
        var externalSignal = options.signal || null;
        var abortFromExternal = function () { controller.abort(); };
        if (externalSignal) {
            if (externalSignal.aborted) {
                controller.abort();
            } else {
                externalSignal.addEventListener("abort", abortFromExternal, { once: true });
            }
        }

        var headers = new Headers(options.headers || {});
        headers.set("Accept", "application/x-ndjson");
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
                signal: controller.signal,
                credentials: "same-origin"
            });
            if (!response.ok) {
                var failure = {};
                try {
                    failure = await response.json();
                } catch (_parseFailure) {
                    throw new ApiError(
                        "服务返回了无效响应", "invalid_response", response.status
                    );
                }
                var responseError = failure.error || {};
                if (response.status === 401 && responseError.code === "unauthorized") {
                    window.dispatchEvent(new CustomEvent("jarvis:auth-required"));
                }
                throw new ApiError(
                    responseError.message || "请求失败",
                    responseError.code,
                    response.status
                );
            }
            if (!response.body || !response.body.getReader) {
                throw new ApiError(
                    "当前浏览器不支持流式响应", "stream_unsupported", 0
                );
            }

            var reader = response.body.getReader();
            var decoder = new TextDecoder("utf-8");
            var pending = "";
            var completed = null;

            async function consumeLine(line) {
                if (!line.trim()) {
                    return;
                }
                var event;
                try {
                    event = JSON.parse(line);
                } catch (_parseEvent) {
                    throw new ApiError(
                        "流式响应格式无效", "invalid_stream_event", response.status
                    );
                }
                if (event.type === "error") {
                    var streamError = event.error || {};
                    if (streamError.code === "unauthorized") {
                        window.dispatchEvent(new CustomEvent("jarvis:auth-required"));
                    }
                    throw new ApiError(
                        streamError.message || "请求失败",
                        streamError.code,
                        response.status
                    );
                }
                if (typeof onEvent === "function") {
                    await onEvent(event);
                }
                if (event.type === "done") {
                    completed = event;
                }
            }

            while (true) {
                var chunk = await reader.read();
                pending += decoder.decode(chunk.value || new Uint8Array(), {
                    stream: !chunk.done
                });
                var lines = pending.split("\n");
                pending = lines.pop();
                for (var index = 0; index < lines.length; index += 1) {
                    await consumeLine(lines[index]);
                }
                if (chunk.done) {
                    break;
                }
            }
            if (pending.trim()) {
                await consumeLine(pending);
            }
            if (!completed) {
                throw new ApiError(
                    "响应流意外中断", "stream_interrupted", response.status
                );
            }
            return completed;
        } catch (error) {
            if (error.name === "AbortError") {
                throw new ApiError(
                    timedOut ? "请求超时，请稍后重试" : "请求已取消",
                    timedOut ? "timeout" : "cancelled",
                    timedOut ? 408 : 0
                );
            }
            throw error;
        } finally {
            window.clearTimeout(timeout);
            if (externalSignal) {
                externalSignal.removeEventListener("abort", abortFromExternal);
            }
        }
    };

    window.JarvisApiClient = ApiClient;
    window.JarvisApiError = ApiError;
}());
