(function () {
    "use strict";

    var STATES = {
        idle: { label: "待命", code: "STANDBY", detail: "SYSTEM READY" },
        listening: { label: "聆听中", code: "LISTENING", detail: "VOICE CHANNEL OPEN" },
        thinking: { label: "处理中", code: "PROCESSING", detail: "COGNITIVE LOAD ACTIVE" },
        speaking: { label: "响应中", code: "SPEAKING", detail: "VOICE OUTPUT ACTIVE" },
        executing: { label: "执行中", code: "EXECUTING", detail: "MODULE OPERATION" },
        error: { label: "异常", code: "ERROR", detail: "RECOVERY IN PROGRESS" }
    };

    function JarvisStateMachine() {
        this.events = document.createDocumentFragment();
        this.state = "idle";
        this.operationId = 0;
        this.resetTimer = null;
    }

    JarvisStateMachine.prototype.addEventListener = function (type, listener) {
        this.events.addEventListener(type, listener);
    };

    JarvisStateMachine.prototype.removeEventListener = function (type, listener) {
        this.events.removeEventListener(type, listener);
    };

    JarvisStateMachine.prototype.begin = function (state) {
        this.operationId += 1;
        this.set(state, { operationId: this.operationId });
        return this.operationId;
    };

    JarvisStateMachine.prototype.set = function (state, options) {
        options = options || {};
        if (!STATES[state]) {
            throw new Error("Unknown JARVIS state: " + state);
        }
        if (options.operationId && options.operationId !== this.operationId) {
            return false;
        }
        window.clearTimeout(this.resetTimer);
        this.resetTimer = null;
        this.state = state;
        this.events.dispatchEvent(new CustomEvent("change", {
            detail: {
                state: state,
                config: STATES[state],
                operationId: this.operationId
            }
        }));
        if (options.duration) {
            var self = this;
            this.resetTimer = window.setTimeout(function () {
                self.set("idle", { operationId: options.operationId || self.operationId });
            }, options.duration);
        }
        return true;
    };

    JarvisStateMachine.prototype.fail = function (operationId) {
        return this.set("error", { operationId: operationId, duration: 2200 });
    };

    JarvisStateMachine.prototype.complete = function (operationId) {
        return this.set("idle", { operationId: operationId });
    };

    JarvisStateMachine.STATES = STATES;
    window.JarvisStateMachine = JarvisStateMachine;
}());
