(function () {
    "use strict";

    var api = new window.JarvisApiClient();
    var machine = new window.JarvisStateMachine();
    var face = null;
    var sessionId = "";
    var requestPending = false;
    var voiceEnabled = window.localStorage.getItem("jarvis.voiceEnabled") === "true";
    var voiceAvailable = false;
    var voiceUnavailableReason = "精确语音合成尚未配置";
    var speechAudioContext = null;
    var activeSpeech = null;
    var activeSpeechQueue = null;
    var recognition = null;
    var microphoneStream = null;
    var audioContext = null;
    var audioFrame = null;
    var listeningOperation = 0;
    var mobileQuery = window.matchMedia("(max-width: 700px)");

    var elements = {};

    function bindElements() {
        [
            "app-shell", "face-canvas", "webgl-fallback", "status-label",
            "status-light", "model-label", "face-state-label", "face-state-detail",
            "conversation-panel", "workspace-panel", "workspace-title",
            "workspace-kicker", "close-workspace", "messages", "chat-form",
            "user-input", "send-button", "mic-button", "voice-button", "clear-chat",
            "session-stat", "interaction-stat", "database-stat", "toast-region",
            "run-learning", "learning-results", "learning-analysis",
            "refresh-evolution", "evolution-results", "evolution-cases",
            "evolution-best", "memory-form", "memory-key", "memory-value",
            "retrieve-memory", "memory-result", "database-results", "skill-count",
            "skills-results", "mine-skills", "auth-dialog", "auth-form", "api-token"
        ].forEach(function (id) {
            elements[id] = document.getElementById(id);
        });
    }

    function initializeIcons() {
        if (window.lucide) {
            window.lucide.createIcons({
                attrs: { "aria-hidden": "true" }
            });
        }
    }

    function replaceButtonIcon(button, name) {
        var icon = document.createElement("i");
        icon.setAttribute("data-lucide", name);
        button.replaceChildren(icon);
        initializeIcons();
    }

    function initializeFace() {
        try {
            face = new window.JarvisFace(elements["face-canvas"]);
        } catch (error) {
            elements["face-canvas"].hidden = true;
            elements["webgl-fallback"].hidden = false;
            showToast("3D 人脸渲染不可用，已切换兼容模式", "error");
        }
    }

    function applyState(event) {
        var detail = event.detail;
        elements["app-shell"].dataset.state = detail.state;
        elements["status-label"].textContent = detail.config.label;
        elements["face-state-label"].textContent = detail.config.code;
        elements["face-state-detail"].textContent = detail.config.detail;
        if (face) {
            face.setState(detail.state);
        }
    }

    function showToast(message, kind) {
        var toast = document.createElement("div");
        toast.className = "toast" + (kind === "error" ? " error" : "");
        toast.textContent = String(message || "操作失败");
        elements["toast-region"].appendChild(toast);
        window.setTimeout(function () { toast.remove(); }, 3600);
    }

    function setBusy(busy) {
        requestPending = busy;
        elements["send-button"].disabled = busy;
        elements["clear-chat"].disabled = busy;
        elements["mic-button"].disabled = busy;
        elements["user-input"].disabled = busy;
        document.querySelectorAll(".mode-tab").forEach(function (button) {
            button.disabled = busy;
        });
    }

    function addFeedbackControls(message, interactionId, currentValue) {
        var controls = document.createElement("div");
        controls.className = "message-feedback";
        var options = [
            { value: true, icon: "thumbs-up", label: "标记为有用" },
            { value: false, icon: "thumbs-down", label: "标记为无用" }
        ];
        var buttons = [];

        function applySelection(value) {
            buttons.forEach(function (button) {
                button.setAttribute(
                    "aria-pressed", String(button.feedbackValue === value)
                );
            });
        }

        options.forEach(function (option) {
            var button = document.createElement("button");
            button.type = "button";
            button.className = "feedback-button";
            button.feedbackValue = option.value;
            button.title = option.label;
            button.setAttribute("aria-label", option.label);
            button.setAttribute("aria-pressed", "false");
            var icon = document.createElement("i");
            icon.setAttribute("data-lucide", option.icon);
            button.appendChild(icon);
            button.addEventListener("click", async function () {
                buttons.forEach(function (item) { item.disabled = true; });
                try {
                    await api.request("/api/feedback", {
                        method: "POST",
                        body: {
                            session_id: sessionId,
                            interaction_id: interactionId,
                            helpful: option.value
                        }
                    });
                    applySelection(option.value);
                } catch (error) {
                    showToast(error.message, "error");
                } finally {
                    buttons.forEach(function (item) { item.disabled = false; });
                }
            });
            buttons.push(button);
            controls.appendChild(button);
        });
        if (currentValue === true || currentValue === false) {
            applySelection(currentValue);
        }
        message.appendChild(controls);
        initializeIcons();
    }

    function addMessage(role, content, pending, interactionId, helpful) {
        var message = document.createElement("article");
        message.className = "message " + role + (pending ? " pending" : "");
        var label = document.createElement("span");
        label.className = "message-role";
        label.textContent = role === "user" ? "YOU" : "JARVIS";
        var body = document.createElement("span");
        body.className = "message-body";
        body.textContent = content;
        message.append(label, body);
        if (role === "assistant" && interactionId && !pending) {
            addFeedbackControls(message, interactionId, helpful);
        }
        elements.messages.appendChild(message);
        elements.messages.scrollTop = elements.messages.scrollHeight;
        return message;
    }

    function resizeComposer() {
        var input = elements["user-input"];
        input.style.height = "auto";
        input.style.height = Math.min(input.scrollHeight, 120) + "px";
    }

    function validSession(value) {
        return /^[a-f0-9]{32}$/.test(value || "");
    }

    async function createSession() {
        var storedUser = window.localStorage.getItem("jarvis.userId") || "";
        var data = await api.request("/api/session", {
            method: "POST",
            body: validSession(storedUser) ? { user_id: storedUser } : {}
        });
        sessionId = data.session_id;
        window.localStorage.setItem("jarvis.sessionId", sessionId);
        window.localStorage.setItem("jarvis.userId", data.user_id);
        elements["session-stat"].textContent = sessionId.slice(-6).toUpperCase();
        return sessionId;
    }

    async function ensureSession() {
        var stored = window.localStorage.getItem("jarvis.sessionId") || "";
        if (!validSession(stored)) {
            return createSession();
        }
        sessionId = stored;
        try {
            await loadHistory();
        } catch (error) {
            if (error.code !== "invalid_session") {
                throw error;
            }
            await createSession();
        }
        elements["session-stat"].textContent = sessionId.slice(-6).toUpperCase();
        return sessionId;
    }

    async function loadHistory() {
        if (!sessionId) {
            return;
        }
        var data = await api.request(
            "/api/chat/history?session_id=" + encodeURIComponent(sessionId) + "&limit=50"
        );
        elements.messages.replaceChildren();
        data.history.forEach(function (item) {
            addMessage(
                item.role, item.content, false, item.interaction_id, item.helpful
            );
        });
    }

    async function loadStatus() {
        var data = await api.request("/api/status");
        elements["model-label"].textContent = data.model;
        elements["interaction-stat"].textContent = String(data.interactions);
        elements["database-stat"].textContent = data.db_size_kb + " KB";
        if (!data.llm_available) {
            elements["model-label"].textContent = "MODEL OFFLINE";
        }
        var speech = data.speech || {};
        voiceAvailable = Boolean(speech.available);
        voiceUnavailableReason = speech.reason || "精确语音合成尚未配置";
        updateVoiceButton();
    }

    function appendResponseText(messageElement, text) {
        var body = messageElement.querySelector(".message-body");
        if (messageElement.classList.contains("pending")) {
            messageElement.classList.remove("pending");
            body.textContent = "";
        }
        body.textContent += String(text || "");
        elements.messages.scrollTop = elements.messages.scrollHeight;
    }

    function resetResponseText(messageElement) {
        messageElement.classList.add("pending");
        messageElement.querySelector(".message-body").textContent = "正在处理";
    }

    function speechChunkBoundary(text, finalChunk) {
        var hardLimit = 220;
        var scanLimit = Math.min(text.length, hardLimit);
        var strong = "。！？!?\n";
        var soft = "；;，,：:";
        for (var index = 0; index < scanLimit; index += 1) {
            if (strong.indexOf(text[index]) !== -1 && index >= 1) {
                return index + 1;
            }
            if (soft.indexOf(text[index]) !== -1 && index >= 31) {
                return index + 1;
            }
        }
        if (text.length >= hardLimit) {
            var candidate = text.slice(0, hardLimit + 1);
            var boundary = Math.max(
                candidate.lastIndexOf(" "),
                candidate.lastIndexOf("\t"),
                candidate.lastIndexOf("，"),
                candidate.lastIndexOf(","),
                candidate.lastIndexOf("；"),
                candidate.lastIndexOf(";")
            );
            return boundary >= 80 ? boundary + 1 : hardLimit;
        }
        return finalChunk ? text.length : 0;
    }

    function ensureSpeechAudioContext() {
        var Context = window.AudioContext || window.webkitAudioContext;
        if (!Context) {
            return null;
        }
        if (!speechAudioContext || speechAudioContext.state === "closed") {
            speechAudioContext = new Context();
        }
        if (speechAudioContext.state === "suspended") {
            speechAudioContext.resume().catch(function () {});
        }
        return speechAudioContext;
    }

    function cancelSpeechPlayback() {
        if (activeSpeechQueue && activeSpeechQueue.cancel) {
            var queue = activeSpeechQueue;
            activeSpeechQueue = null;
            queue.cancel(true);
        }
        if (activeSpeech && activeSpeech.finish) {
            activeSpeech.finish(false);
        }
        activeSpeech = null;
        if (face) {
            face.stopSpeaking();
        }
    }

    function decodeAudio(base64) {
        var binary = window.atob(base64);
        var bytes = new Uint8Array(binary.length);
        for (var index = 0; index < binary.length; index += 1) {
            bytes[index] = binary.charCodeAt(index);
        }
        return bytes.buffer;
    }

    async function playSpeechSegment(payload, operationId, options) {
        options = options || {};
        var context = ensureSpeechAudioContext();
        if (!context) {
            throw new Error("当前浏览器不支持音频播放");
        }
        var buffer = await context.decodeAudioData(
            decodeAudio(payload.audio_base64).slice(0)
        );
        if (operationId !== machine.operationId || !voiceEnabled) {
            return false;
        }

        return new Promise(function (resolve) {
            var source = context.createBufferSource();
            var timeline = Array.isArray(payload.visemes) ? payload.visemes : [];
            var useAudioAnalysis = (
                payload.viseme_source === "audio-analysis" || !timeline.length
            );
            var analyser = null;
            var timeSamples = null;
            var frequencySamples = null;
            var audioEnvelope = 0;
            var lastAudioAnalysisAt = -100;
            var timelineIndex = -1;
            var animationFrame = null;
            var settled = false;
            var startedAt = context.currentTime;
            var durationMs = Math.max(1, buffer.duration * 1000);

            function finish(completed) {
                if (settled) {
                    return;
                }
                settled = true;
                if (animationFrame) {
                    window.cancelAnimationFrame(animationFrame);
                }
                source.onended = null;
                try {
                    source.stop();
                } catch (_stopError) {
                    // BufferSource may already have reached its natural end.
                }
                if (activeSpeech && activeSpeech.source === source) {
                    activeSpeech = null;
                }
                if (face) {
                    face.stopSpeaking();
                }
                if (completed && typeof options.onProgress === "function") {
                    options.onProgress(durationMs, durationMs);
                }
                resolve(completed);
            }

            function animateVisemes() {
                if (
                    settled || operationId !== machine.operationId
                    || !voiceEnabled
                ) {
                    finish(false);
                    return;
                }
                var elapsedMs = Math.max(0, context.currentTime - startedAt) * 1000;
                if (typeof options.onProgress === "function") {
                    options.onProgress(Math.min(elapsedMs, durationMs), durationMs);
                }
                if (useAudioAnalysis && analyser && elapsedMs - lastAudioAnalysisAt >= 40) {
                    lastAudioAnalysisAt = elapsedMs;
                    analyser.getByteTimeDomainData(timeSamples);
                    var sumSquares = 0;
                    for (var sample = 0; sample < timeSamples.length; sample += 1) {
                        var amplitude = (timeSamples[sample] - 128) / 128;
                        sumSquares += amplitude * amplitude;
                    }
                    var rms = Math.sqrt(sumSquares / timeSamples.length);
                    var targetEnvelope = Math.max(
                        0, Math.min(1, (rms - 0.008) * 11)
                    );
                    audioEnvelope = audioEnvelope * 0.58 + targetEnvelope * 0.42;
                    var reactiveViseme = 0;
                    if (audioEnvelope >= 0.035) {
                        analyser.getByteFrequencyData(frequencySamples);
                        var magnitudeTotal = 0;
                        var weightedFrequency = 0;
                        for (
                            var bin = 1;
                            bin < frequencySamples.length;
                            bin += 1
                        ) {
                            magnitudeTotal += frequencySamples[bin];
                            weightedFrequency += frequencySamples[bin] * bin;
                        }
                        var centroid = magnitudeTotal
                            ? weightedFrequency
                                / (magnitudeTotal * (frequencySamples.length - 1))
                            : 0;
                        if (audioEnvelope < 0.13) {
                            reactiveViseme = 21;
                        } else if (centroid > 0.52) {
                            reactiveViseme = audioEnvelope > 0.48 ? 16 : 15;
                        } else if (centroid < 0.2) {
                            reactiveViseme = audioEnvelope > 0.55 ? 3 : 7;
                        } else if (audioEnvelope > 0.72) {
                            reactiveViseme = 2;
                        } else if (audioEnvelope > 0.48) {
                            reactiveViseme = 1;
                        } else if (centroid > 0.34) {
                            reactiveViseme = 6;
                        } else {
                            reactiveViseme = 4;
                        }
                    }
                    if (face) {
                        face.setViseme(reactiveViseme);
                    }
                } else if (!useAudioAnalysis) {
                    while (
                        timelineIndex + 1 < timeline.length
                        && Number(timeline[timelineIndex + 1].offset_ms) <= elapsedMs
                    ) {
                        timelineIndex += 1;
                        if (face) {
                            face.setViseme(Number(timeline[timelineIndex].id));
                        }
                    }
                }
                animationFrame = window.requestAnimationFrame(animateVisemes);
            }

            source.buffer = buffer;
            if (useAudioAnalysis) {
                analyser = context.createAnalyser();
                analyser.fftSize = 256;
                analyser.smoothingTimeConstant = 0.58;
                timeSamples = new Uint8Array(analyser.fftSize);
                frequencySamples = new Uint8Array(analyser.frequencyBinCount);
                source.connect(analyser);
                analyser.connect(context.destination);
            } else {
                source.connect(context.destination);
            }
            source.onended = function () { finish(true); };
            activeSpeech = { source: source, finish: finish };
            machine.set("speaking", { operationId: operationId });
            if (face) {
                face.startSpeaking();
            }
            source.start(0);
            startedAt = context.currentTime;
            if (typeof options.onStart === "function") {
                options.onStart();
            }
            animateVisemes();
        });
    }

    function createSpeechStream(messageElement, operationId) {
        var body = messageElement.querySelector(".message-body");
        var receivedText = "";
        var visibleText = "";
        var pendingText = "";
        var items = [];
        var playIndex = 0;
        var pumping = false;
        var ended = false;
        var cancelled = false;
        var failed = false;
        var displayStarted = false;
        var settled = false;
        var requestController = new AbortController();
        var resolveCompletion;
        var completion = new Promise(function (resolve) {
            resolveCompletion = resolve;
        });
        var streamApi = null;

        function settle(value) {
            if (settled) {
                return;
            }
            settled = true;
            if (activeSpeechQueue === streamApi) {
                activeSpeechQueue = null;
            }
            resolveCompletion(value);
        }

        function beginDisplay() {
            if (displayStarted) {
                return;
            }
            displayStarted = true;
            messageElement.classList.remove("pending");
            body.textContent = "";
        }

        function render(text) {
            if (!text) {
                return;
            }
            beginDisplay();
            visibleText += text;
            body.textContent = visibleText;
            elements.messages.scrollTop = elements.messages.scrollHeight;
        }

        function revealReceivedText() {
            beginDisplay();
            visibleText = receivedText;
            body.textContent = receivedText;
            elements.messages.scrollTop = elements.messages.scrollHeight;
        }

        function enqueue(text) {
            if (!text) {
                return;
            }
            items.push({
                text: text,
                speechText: text.trim(),
                request: null
            });
        }

        function flushChunks(finalChunk) {
            var boundary = speechChunkBoundary(pendingText, finalChunk);
            while (boundary > 0) {
                enqueue(pendingText.slice(0, boundary));
                pendingText = pendingText.slice(boundary);
                boundary = speechChunkBoundary(pendingText, finalChunk);
            }
        }

        function requestItem(item) {
            if (item.request) {
                return;
            }
            if (!item.speechText) {
                item.request = Promise.resolve({ payload: null, error: null });
                return;
            }
            item.request = api.request("/api/speech", {
                method: "POST",
                body: { text: item.speechText, session_id: sessionId },
                timeout: 90000,
                signal: requestController.signal
            }).then(function (payload) {
                return { payload: payload, error: null };
            }, function (error) {
                return { payload: null, error: error };
            });
        }

        function prefetch() {
            var end = Math.min(items.length, playIndex + 2);
            for (var index = playIndex; index < end; index += 1) {
                requestItem(items[index]);
            }
        }

        function failSpeech(error) {
            if (failed || cancelled) {
                return;
            }
            failed = true;
            requestController.abort();
            if (activeSpeech && activeSpeech.finish) {
                activeSpeech.finish(false);
            }
            revealReceivedText();
            showToast(error.message || "语音播放失败", "error");
            settle(false);
        }

        async function pump() {
            if (pumping || cancelled || failed) {
                return;
            }
            pumping = true;
            try {
                while (!cancelled && !failed && playIndex < items.length) {
                    prefetch();
                    var item = items[playIndex];
                    var outcome = await item.request;
                    if (cancelled || failed) {
                        break;
                    }
                    if (outcome.error) {
                        throw outcome.error;
                    }
                    if (!outcome.payload) {
                        render(item.text);
                        playIndex += 1;
                        continue;
                    }

                    var revealed = 0;
                    var played = await playSpeechSegment(
                        outcome.payload,
                        operationId,
                        {
                            onStart: beginDisplay,
                            onProgress: function (elapsedMs, durationMs) {
                                var progress = Math.min(1, elapsedMs / durationMs);
                                var target = Math.min(
                                    item.text.length,
                                    Math.floor(item.text.length * progress)
                                );
                                if (target > revealed) {
                                    render(item.text.slice(revealed, target));
                                    revealed = target;
                                }
                            }
                        }
                    );
                    if (!played) {
                        cancelled = true;
                        settle(false);
                        break;
                    }
                    if (revealed < item.text.length) {
                        render(item.text.slice(revealed));
                    }
                    playIndex += 1;
                }
            } catch (error) {
                failSpeech(error);
            } finally {
                pumping = false;
                if (!cancelled && !failed && playIndex < items.length) {
                    window.setTimeout(pump, 0);
                } else if (ended && playIndex >= items.length) {
                    settle(true);
                }
            }
        }

        streamApi = {
            push: function (text) {
                if (cancelled) {
                    return false;
                }
                text = String(text || "");
                receivedText += text;
                if (failed) {
                    revealReceivedText();
                    return true;
                }
                pendingText += text;
                flushChunks(false);
                prefetch();
                pump();
                return true;
            },
            finish: function () {
                ended = true;
                if (cancelled) {
                    settle(false);
                    return completion;
                }
                if (failed) {
                    revealReceivedText();
                    settle(false);
                    return completion;
                }
                flushChunks(true);
                prefetch();
                pump();
                if (!pumping && playIndex >= items.length) {
                    settle(true);
                }
                return completion;
            },
            cancel: function (revealText) {
                if (cancelled) {
                    return;
                }
                cancelled = true;
                requestController.abort();
                if (activeSpeech && activeSpeech.finish) {
                    activeSpeech.finish(false);
                }
                if (revealText) {
                    revealReceivedText();
                }
                settle(false);
            }
        };
        activeSpeechQueue = streamApi;
        return streamApi;
    }

    async function requestChatResponse(message, onEvent) {
        try {
            return await api.stream("/api/chat/stream", {
                method: "POST",
                body: { message: message, session_id: sessionId },
                timeout: 610000
            }, onEvent);
        } catch (error) {
            if (error.status !== 404 || error.code !== "not_found") {
                throw error;
            }
            var legacy = await api.request("/api/chat", {
                method: "POST",
                body: { message: message, session_id: sessionId },
                timeout: 610000
            });
            var start = {
                type: "start",
                interaction_id: legacy.interaction_id,
                session_id: legacy.session_id
            };
            var delta = { type: "delta", text: legacy.response };
            var done = {
                type: "done",
                response: legacy.response,
                interaction_id: legacy.interaction_id,
                session_id: legacy.session_id
            };
            await onEvent(start);
            await onEvent(delta);
            await onEvent(done);
            return done;
        }
    }

    async function sendMessage() {
        var message = elements["user-input"].value.trim();
        if (!message || requestPending) {
            return;
        }
        var operationId = machine.begin("thinking");
        if (voiceEnabled) {
            ensureSpeechAudioContext();
        }
        setBusy(true);
        var responseNode = null;
        var speechStream = null;
        var fullResponse = "";
        var interactionId = null;

        try {
            if (!sessionId) {
                await ensureSession();
            }
            cancelSpeechPlayback();
            addMessage("user", message, false);
            elements["user-input"].value = "";
            resizeComposer();
            responseNode = addMessage("assistant", "正在处理", true);
            if (voiceEnabled && voiceAvailable) {
                speechStream = createSpeechStream(responseNode, operationId);
            } else if (voiceEnabled) {
                showToast(voiceUnavailableReason, "error");
            }

            var data = await requestChatResponse(message, function (event) {
                if (event.type === "start") {
                    interactionId = event.interaction_id;
                    return;
                }
                if (event.type === "reset") {
                    fullResponse = "";
                    if (speechStream) {
                        speechStream.cancel(false);
                        speechStream = voiceEnabled && voiceAvailable
                            ? createSpeechStream(responseNode, operationId)
                            : null;
                    }
                    resetResponseText(responseNode);
                    return;
                }
                if (event.type !== "delta") {
                    return;
                }
                var text = String(event.text || "");
                var firstDelta = fullResponse.length === 0;
                fullResponse += text;
                if (firstDelta) {
                    machine.set("speaking", { operationId: operationId });
                }
                if (!speechStream || !speechStream.push(text)) {
                    appendResponseText(responseNode, text);
                }
            });

            interactionId = data.interaction_id || interactionId;
            var canonicalResponse = String(data.response || fullResponse);
            if (!speechStream) {
                responseNode.classList.remove("pending");
                responseNode.querySelector(".message-body").textContent = canonicalResponse;
            }

            function completeResponse() {
                if (responseNode.isConnected && interactionId) {
                    addFeedbackControls(responseNode, interactionId, null);
                }
                machine.complete(operationId);
            }

            if (speechStream) {
                speechStream.finish().then(completeResponse);
            } else {
                completeResponse();
            }
            loadStatus().catch(function () {});
        } catch (error) {
            if (speechStream) {
                speechStream.cancel(false);
            }
            if (responseNode) {
                responseNode.remove();
                addMessage("assistant", error.message || "请求失败", false);
            }
            machine.fail(operationId);
            showToast(error.message || "请求失败", "error");
            if (error.code === "invalid_session") {
                window.localStorage.removeItem("jarvis.sessionId");
                sessionId = "";
            }
        } finally {
            setBusy(false);
            elements["user-input"].focus();
        }
    }

    function updateVoiceButton() {
        elements["voice-button"].setAttribute("aria-pressed", String(voiceEnabled));
        elements["voice-button"].setAttribute(
            "aria-label", voiceEnabled ? "关闭语音播报" : "开启语音播报"
        );
        elements["voice-button"].title = voiceAvailable
            ? (voiceEnabled ? "关闭同步语音" : "开启同步语音")
            : voiceUnavailableReason;
        replaceButtonIcon(elements["voice-button"], voiceEnabled ? "volume-2" : "volume-x");
    }

    function stopAudioMeter() {
        if (audioFrame) {
            window.cancelAnimationFrame(audioFrame);
            audioFrame = null;
        }
        if (microphoneStream) {
            microphoneStream.getTracks().forEach(function (track) { track.stop(); });
            microphoneStream = null;
        }
        if (audioContext) {
            audioContext.close().catch(function () {});
            audioContext = null;
        }
        if (face) {
            face.setAudioLevel(0);
        }
    }

    async function startAudioMeter() {
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
            return;
        }
        microphoneStream = await navigator.mediaDevices.getUserMedia({ audio: true });
        var Context = window.AudioContext || window.webkitAudioContext;
        if (!Context) {
            return;
        }
        audioContext = new Context();
        var analyser = audioContext.createAnalyser();
        analyser.fftSize = 256;
        audioContext.createMediaStreamSource(microphoneStream).connect(analyser);
        var samples = new Uint8Array(analyser.fftSize);
        function measure() {
            analyser.getByteTimeDomainData(samples);
            var sum = 0;
            for (var index = 0; index < samples.length; index += 1) {
                var value = (samples[index] - 128) / 128;
                sum += value * value;
            }
            if (face) {
                face.setAudioLevel(Math.min(1, Math.sqrt(sum / samples.length) * 4));
            }
            audioFrame = window.requestAnimationFrame(measure);
        }
        measure();
    }

    async function toggleListening() {
        if (requestPending) {
            return;
        }
        if (recognition) {
            recognition.stop();
            return;
        }
        var Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!Recognition) {
            showToast("当前浏览器不支持语音识别", "error");
            return;
        }
        recognition = new Recognition();
        recognition.lang = "zh-CN";
        recognition.interimResults = true;
        recognition.continuous = false;
        listeningOperation = machine.begin("listening");
        elements["mic-button"].setAttribute("aria-pressed", "true");
        var transcript = "";
        var recognitionFailed = false;
        try {
            await startAudioMeter();
        } catch (error) {
            recognition = null;
            stopAudioMeter();
            machine.fail(listeningOperation);
            elements["mic-button"].setAttribute("aria-pressed", "false");
            showToast("无法访问麦克风", "error");
            return;
        }
        recognition.onresult = function (event) {
            transcript = "";
            for (var index = 0; index < event.results.length; index += 1) {
                transcript += event.results[index][0].transcript;
            }
            elements["user-input"].value = transcript;
            resizeComposer();
        };
        recognition.onerror = function (event) {
            if (event.error !== "no-speech" && event.error !== "aborted") {
                recognitionFailed = true;
                machine.fail(listeningOperation);
                showToast("语音识别失败", "error");
            }
        };
        recognition.onend = function () {
            recognition = null;
            stopAudioMeter();
            elements["mic-button"].setAttribute("aria-pressed", "false");
            if (recognitionFailed) {
                return;
            }
            if (transcript.trim()) {
                sendMessage();
            } else {
                machine.complete(listeningOperation);
            }
        };
        try {
            recognition.start();
        } catch (error) {
            recognition = null;
            stopAudioMeter();
            elements["mic-button"].setAttribute("aria-pressed", "false");
            machine.fail(listeningOperation);
            showToast("无法启动语音识别", "error");
        }
    }

    function createResultRow(title, meta) {
        var row = document.createElement("div");
        row.className = "result-row";
        var strong = document.createElement("strong");
        strong.textContent = String(title);
        var span = document.createElement("span");
        span.textContent = String(meta || "");
        row.append(strong, span);
        return row;
    }

    function setLoading(container) {
        var loading = document.createElement("div");
        loading.className = "loading-row";
        loading.textContent = "LOADING";
        container.replaceChildren(loading);
    }

    function setEmpty(container, text) {
        var empty = document.createElement("div");
        empty.className = "empty-row";
        empty.textContent = text;
        container.replaceChildren(empty);
    }

    async function withModuleState(action) {
        var operationId = machine.begin("executing");
        try {
            await action();
            machine.complete(operationId);
        } catch (error) {
            machine.fail(operationId);
            showToast(error.message, "error");
        }
    }

    function loadLearning() {
        return withModuleState(async function () {
            setLoading(elements["learning-results"]);
            elements["learning-analysis"].hidden = true;
            var data = await api.request("/api/learn", {
                method: "POST",
                timeout: 110000
            });
            if (!data.patterns.length) {
                setEmpty(elements["learning-results"], "暂无模式");
            } else {
                var fragment = document.createDocumentFragment();
                data.patterns.forEach(function (pattern) {
                    fragment.appendChild(createResultRow(
                        pattern.sequence,
                        Math.round(pattern.support * 100) + "% / " + Math.round(pattern.confidence * 100) + "%"
                    ));
                });
                elements["learning-results"].replaceChildren(fragment);
            }
            if (data.ai_analysis) {
                elements["learning-analysis"].textContent = data.ai_analysis;
                elements["learning-analysis"].hidden = false;
            }
        });
    }

    function loadEvolution() {
        return withModuleState(async function () {
            setLoading(elements["evolution-results"]);
            var data = await api.request("/api/evolve");
            elements["evolution-cases"].textContent = String(data.available_cases);
            if (!data.history.length) {
                elements["evolution-best"].textContent = "--";
                setEmpty(elements["evolution-results"], "暂无进化记录");
                return;
            }
            var best = Math.max.apply(null, data.history.map(function (item) {
                return Number(item.fitness_score) || 0;
            }));
            elements["evolution-best"].textContent = best.toFixed(3);
            var fragment = document.createDocumentFragment();
            data.history.forEach(function (item) {
                var row = createResultRow(
                    "GEN " + item.generation,
                    Number(item.fitness_score || 0).toFixed(3)
                );
                var details = document.createElement("details");
                details.className = "skill-details";
                var summary = document.createElement("summary");
                summary.textContent = "Prompt 详情";
                var prompt = document.createElement("pre");
                prompt.textContent = item.content || "";
                details.append(summary, prompt);
                var toggle = document.createElement("input");
                toggle.type = "checkbox";
                toggle.className = "skill-toggle evolution-toggle";
                toggle.checked = Boolean(item.approved);
                toggle.disabled = !item.approved;
                toggle.setAttribute("aria-label", "批准第 " + item.generation + " 代 Prompt");
                details.addEventListener("toggle", function () {
                    if (details.open && !item.approved) {
                        toggle.disabled = false;
                    }
                });
                toggle.addEventListener("change", async function () {
                    var nextValue = toggle.checked;
                    toggle.disabled = true;
                    try {
                        await api.request("/api/evolve/approve", {
                            method: "POST",
                            body: {
                                id: item.id,
                                approved: nextValue,
                                reviewed: true
                            }
                        });
                        if (nextValue) {
                            document.querySelectorAll(".evolution-toggle").forEach(
                                function (other) {
                                    if (other !== toggle) {
                                        other.checked = false;
                                    }
                                }
                            );
                        }
                        item.approved = nextValue;
                    } catch (error) {
                        toggle.checked = !nextValue;
                        showToast(error.message, "error");
                    } finally {
                        toggle.disabled = false;
                    }
                });
                row.append(toggle, details);
                fragment.appendChild(row);
            });
            elements["evolution-results"].replaceChildren(fragment);
        });
    }

    function loadDatabase() {
        return withModuleState(async function () {
            var loadingRow = document.createElement("tr");
            var cell = document.createElement("td");
            cell.colSpan = 3;
            cell.className = "loading-row";
            cell.textContent = "LOADING";
            loadingRow.appendChild(cell);
            elements["database-results"].replaceChildren(loadingRow);
            var data = await api.request("/api/database/stats");
            var fragment = document.createDocumentFragment();
            data.tables.forEach(function (table) {
                var row = document.createElement("tr");
                [table.name, table.count, table.description].forEach(function (value) {
                    var field = document.createElement("td");
                    field.textContent = String(value);
                    row.appendChild(field);
                });
                fragment.appendChild(row);
            });
            elements["database-results"].replaceChildren(fragment);
        });
    }

    function loadSkills() {
        return withModuleState(async function () {
            setLoading(elements["skills-results"]);
            var data = await api.request("/api/skills");
            elements["skill-count"].textContent = String(data.skills.length);
            if (!data.skills.length) {
                setEmpty(elements["skills-results"], "暂无技能");
                return;
            }
            var fragment = document.createDocumentFragment();
            data.skills.forEach(function (skill) {
                var row = createResultRow(
                    skill.name, (skill.trigger_count || 0) + " TRIGGERS"
                );
                var details = document.createElement("details");
                details.className = "skill-details";
                var summary = document.createElement("summary");
                summary.textContent = "配置详情";
                var description = document.createElement("p");
                description.textContent = skill.description || "--";
                var keywords = document.createElement("p");
                keywords.textContent = "KEYWORDS / " + (skill.trigger_keywords || []).join(", ");
                var prompt = document.createElement("pre");
                prompt.textContent = skill.prompt_template || "";
                details.append(summary, description, keywords, prompt);
                var toggle = document.createElement("input");
                toggle.type = "checkbox";
                toggle.className = "skill-toggle";
                toggle.checked = Boolean(skill.enabled);
                toggle.disabled = !skill.reviewed;
                toggle.setAttribute("aria-label", "启用 " + skill.name);
                details.addEventListener("toggle", function () {
                    if (details.open && !skill.reviewed) {
                        toggle.disabled = false;
                    }
                });
                toggle.addEventListener("change", async function () {
                    var nextValue = toggle.checked;
                    toggle.disabled = true;
                    try {
                        await api.request("/api/skills/toggle", {
                            method: "POST",
                            body: {
                                id: skill.id,
                                enabled: nextValue,
                                reviewed: true
                            }
                        });
                    } catch (error) {
                        toggle.checked = !nextValue;
                        showToast(error.message, "error");
                    } finally {
                        toggle.disabled = false;
                    }
                });
                row.appendChild(toggle);
                row.appendChild(details);
                fragment.appendChild(row);
            });
            elements["skills-results"].replaceChildren(fragment);
        });
    }

    function mineSkills() {
        return withModuleState(async function () {
            elements["mine-skills"].disabled = true;
            try {
                var data = await api.request("/api/skills/mine", {
                    method: "POST",
                    timeout: 120000
                });
                showToast("新增技能 " + data.count + " 个");
                await loadSkills();
            } finally {
                elements["mine-skills"].disabled = false;
            }
        });
    }

    function storeMemory(event) {
        event.preventDefault();
        var key = elements["memory-key"].value.trim();
        var value = elements["memory-value"].value;
        if (!key) {
            showToast("请输入记忆键", "error");
            return;
        }
        return withModuleState(async function () {
            var data = await api.request("/api/memory/store", {
                method: "POST",
                body: { session_id: sessionId, key: key, value: value }
            });
            elements["memory-result"].textContent = "STORED / " + data.key;
            elements["memory-result"].hidden = false;
        });
    }

    function retrieveMemory() {
        var key = elements["memory-key"].value.trim();
        if (!key) {
            showToast("请输入记忆键", "error");
            return;
        }
        return withModuleState(async function () {
            var data = await api.request(
                "/api/memory/retrieve?session_id=" + encodeURIComponent(sessionId) +
                "&key=" + encodeURIComponent(key)
            );
            elements["memory-result"].textContent = data.found
                ? String(data.value)
                : "NOT FOUND";
            elements["memory-result"].hidden = false;
        });
    }

    var MODE_META = {
        learn: ["学习", "PATTERN MODULE", loadLearning],
        evolve: ["进化", "EVOLUTION MODULE", loadEvolution],
        memory: ["记忆", "MEMORY MODULE", null],
        database: ["数据库", "DATA MODULE", loadDatabase],
        skills: ["技能", "SKILL MODULE", loadSkills]
    };

    function applyResponsivePanels(mode) {
        elements["conversation-panel"].hidden = mode !== "chat" && mobileQuery.matches;
    }

    function switchMode(mode) {
        if (requestPending) {
            return;
        }
        document.querySelectorAll(".mode-tab").forEach(function (button) {
            var active = button.dataset.mode === mode;
            button.classList.toggle("active", active);
            button.setAttribute("aria-pressed", String(active));
        });
        var isWorkspace = mode !== "chat";
        elements["workspace-panel"].hidden = !isWorkspace;
        elements["app-shell"].classList.toggle("workspace-open", isWorkspace);
        applyResponsivePanels(mode);
        if (face) {
            face.setWorkspaceOpen(isWorkspace);
        }
        if (!isWorkspace) {
            return;
        }
        document.querySelectorAll(".module-view").forEach(function (view) {
            view.hidden = view.dataset.view !== mode;
        });
        var meta = MODE_META[mode];
        elements["workspace-title"].textContent = meta[0];
        elements["workspace-kicker"].textContent = meta[1];
        if (meta[2]) {
            meta[2]();
        }
    }

    async function newConversation() {
        if (requestPending) {
            return;
        }
        try {
            cancelSpeechPlayback();
            await createSession();
            elements.messages.replaceChildren();
            machine.set("idle");
        } catch (error) {
            showToast(error.message, "error");
        }
    }

    async function bootData() {
        if (document.body.dataset.tokenRequired === "true" && !api.token) {
            elements["auth-dialog"].showModal();
            return;
        }
        try {
            await ensureSession();
            await Promise.all([loadHistory(), loadStatus()]);
        } catch (error) {
            if (error.code !== "unauthorized") {
                showToast(error.message, "error");
                machine.fail(machine.operationId);
            }
        }
    }

    function bindEvents() {
        machine.addEventListener("change", applyState);
        elements["chat-form"].addEventListener("submit", function (event) {
            event.preventDefault();
            sendMessage();
        });
        elements["user-input"].addEventListener("input", resizeComposer);
        elements["user-input"].addEventListener("keydown", function (event) {
            if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
                event.preventDefault();
                sendMessage();
            }
        });
        elements["mic-button"].addEventListener("click", toggleListening);
        elements["voice-button"].addEventListener("click", function () {
            voiceEnabled = !voiceEnabled;
            window.localStorage.setItem("jarvis.voiceEnabled", String(voiceEnabled));
            if (voiceEnabled) {
                ensureSpeechAudioContext();
                if (!voiceAvailable) {
                    showToast(voiceUnavailableReason, "error");
                }
            } else {
                cancelSpeechPlayback();
            }
            updateVoiceButton();
        });
        elements["clear-chat"].addEventListener("click", newConversation);
        elements["close-workspace"].addEventListener("click", function () {
            switchMode("chat");
        });
        document.querySelectorAll(".mode-tab").forEach(function (button) {
            button.addEventListener("click", function () { switchMode(button.dataset.mode); });
        });
        elements["run-learning"].addEventListener("click", loadLearning);
        elements["refresh-evolution"].addEventListener("click", loadEvolution);
        elements["memory-form"].addEventListener("submit", storeMemory);
        elements["retrieve-memory"].addEventListener("click", retrieveMemory);
        elements["mine-skills"].addEventListener("click", mineSkills);
        elements["auth-form"].addEventListener("submit", function (event) {
            event.preventDefault();
            api.setToken(elements["api-token"].value);
            elements["api-token"].value = "";
            elements["auth-dialog"].close();
            bootData();
        });
        window.addEventListener("jarvis:auth-required", function () {
            if (!elements["auth-dialog"].open) {
                elements["auth-dialog"].showModal();
            }
        });
        mobileQuery.addEventListener("change", function () {
            var active = document.querySelector(".mode-tab.active");
            applyResponsivePanels(active ? active.dataset.mode : "chat");
        });
    }

    function initialize() {
        bindElements();
        initializeIcons();
        initializeFace();
        bindEvents();
        updateVoiceButton();
        machine.set("idle");
        resizeComposer();
        bootData();
        window.setInterval(function () {
            if (!document.hidden) {
                loadStatus().catch(function () {});
            }
        }, 30000);
    }

    window.addEventListener("DOMContentLoaded", initialize, { once: true });
}());
