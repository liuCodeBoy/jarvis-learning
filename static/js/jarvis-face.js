(function () {
    "use strict";

    var STATE_STYLE = {
        idle: { color: 0x66ddd8, eye: 1, gazeX: 0, gazeY: 0, tilt: 0, opacity: 0.2 },
        listening: { color: 0x9ce8bd, eye: 1.06, gazeX: 0, gazeY: 0, tilt: -0.018, opacity: 0.28 },
        thinking: { color: 0xffca61, eye: 0.94, gazeX: 0.025, gazeY: 0.02, tilt: 0.014, opacity: 0.24 },
        speaking: { color: 0x66ddd8, eye: 1, gazeX: 0, gazeY: -0.006, tilt: 0, opacity: 0.3 },
        executing: { color: 0x9ce8bd, eye: 0.92, gazeX: 0, gazeY: 0.012, tilt: 0, opacity: 0.28 },
        error: { color: 0xff7373, eye: 0.78, gazeX: 0, gazeY: -0.014, tilt: 0, opacity: 0.32 }
    };

    function clamp(value, minimum, maximum) {
        return Math.max(minimum, Math.min(maximum, value));
    }

    function smoothstep(minimum, maximum, value) {
        var normalized = clamp((value - minimum) / (maximum - minimum), 0, 1);
        return normalized * normalized * (3 - 2 * normalized);
    }

    function glowMaterial(color, opacity) {
        return new THREE.MeshBasicMaterial({
            color: color,
            transparent: true,
            opacity: opacity,
            blending: THREE.AdditiveBlending,
            depthWrite: false,
            side: THREE.DoubleSide
        });
    }

    function almondGeometry(width, height) {
        var shape = new THREE.Shape();
        shape.moveTo(-width, 0);
        shape.quadraticCurveTo(-width * 0.18, height, width, 0);
        shape.quadraticCurveTo(width * 0.14, -height * 0.78, -width, 0);
        return new THREE.ShapeGeometry(shape, 20);
    }

    function JarvisFace(canvas) {
        if (!window.THREE || !THREE.GLTFLoader) {
            throw new Error("Three.js face model loader is unavailable");
        }
        this.canvas = canvas;
        this.modelUrl = canvas.dataset.modelUrl;
        this.reducedMotion = window.matchMedia(
            "(prefers-reduced-motion: reduce)"
        ).matches;
        this.state = "idle";
        this.audioLevel = 0;
        this.smoothedAudio = 0;
        this.speechActive = false;
        this.speechManual = false;
        this.speechText = "";
        this.speechStarted = 0;
        this.speechImpulse = 0;
        this.speechWidth = 1;
        this.currentMouthOpen = 0;
        this.pointer = { x: 0, y: 0 };
        this.clock = new THREE.Clock();
        this.elapsed = 0;
        this.frameAccumulator = 0;
        this.nextBlink = 2.3 + Math.random() * 2.7;
        this.blinkStarted = -1;
        this.workspaceOpen = false;
        this.layoutX = 0;
        this.layoutY = 0;
        this.modelReady = false;
        this.headGeometry = null;
        this.basePositions = null;

        this.renderer = new THREE.WebGLRenderer({
            canvas: canvas,
            antialias: true,
            alpha: true,
            powerPreference: "high-performance"
        });
        this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
        this.renderer.setClearColor(0x000000, 0);
        this.renderer.outputEncoding = THREE.sRGBEncoding;

        this.scene = new THREE.Scene();
        this.camera = new THREE.PerspectiveCamera(31, 1, 0.1, 100);
        this.camera.position.set(0, 0, 5.2);
        this.faceRoot = new THREE.Group();
        this.modelRoot = new THREE.Group();
        this.faceRoot.add(this.modelRoot);
        this.scene.add(this.faceRoot);

        this.scene.add(new THREE.AmbientLight(0x6ca5a5, 0.7));
        var keyLight = new THREE.PointLight(0xc7ffff, 1.1, 10);
        keyLight.position.set(-2.1, 1.7, 3.6);
        this.scene.add(keyLight);
        var rimLight = new THREE.PointLight(0xffd37d, 0.38, 8);
        rimLight.position.set(2.3, -0.5, 2.6);
        this.scene.add(rimLight);

        this.wireMaterial = new THREE.MeshBasicMaterial({
            color: 0x66ddd8,
            wireframe: true,
            transparent: true,
            opacity: 0.2,
            blending: THREE.AdditiveBlending,
            depthWrite: false
        });
        this.surfaceMaterial = new THREE.MeshPhongMaterial({
            color: 0x2c8587,
            emissive: 0x071d20,
            specular: 0xb9eeee,
            shininess: 72,
            transparent: true,
            opacity: 0.075,
            depthWrite: false,
            side: THREE.DoubleSide
        });
        this.pointMaterial = new THREE.PointsMaterial({
            color: 0xd9ffff,
            size: 0.012,
            transparent: true,
            opacity: 0.055,
            blending: THREE.AdditiveBlending,
            depthWrite: false,
            sizeAttenuation: true
        });
        this.eyeMaterial = glowMaterial(0xb9ffff, 0.72);
        this.pupilMaterial = new THREE.MeshBasicMaterial({
            color: 0x02090c,
            transparent: true,
            opacity: 0.92,
            depthWrite: false
        });
        this.materials = [
            this.wireMaterial,
            this.surfaceMaterial,
            this.pointMaterial,
            this.eyeMaterial
        ];

        this._buildLoadingMesh();
        this._buildEyes();
        this._buildMouthRig();
        this._loadHeadModel();
        this.resize();

        var self = this;
        this._resizeHandler = function () { self.resize(); };
        this._pointerHandler = function (event) {
            self.pointer.x = (event.clientX / window.innerWidth - 0.5) * 2;
            self.pointer.y = (0.5 - event.clientY / window.innerHeight) * 2;
        };
        window.addEventListener("resize", this._resizeHandler, { passive: true });
        window.addEventListener("pointermove", this._pointerHandler, { passive: true });
        this.renderer.setAnimationLoop(function () { self._render(); });
    }

    JarvisFace.prototype._buildLoadingMesh = function () {
        var geometry = new THREE.IcosahedronGeometry(1, 4);
        geometry.scale(0.7, 0.92, 0.62);
        this.loadingMesh = new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({
            color: 0x66ddd8,
            wireframe: true,
            transparent: true,
            opacity: 0.12,
            blending: THREE.AdditiveBlending,
            depthWrite: false
        }));
        this.modelRoot.add(this.loadingMesh);
    };

    JarvisFace.prototype._buildEyes = function () {
        this.eyeRig = new THREE.Group();
        this.eyeRig.visible = false;
        this.eyeGroups = [];
        this.irises = [];
        var self = this;
        [-1, 1].forEach(function (side) {
            var eye = new THREE.Group();
            eye.position.set(side * 1.12, 0.53, 2.18);

            var sclera = new THREE.Mesh(
                almondGeometry(0.43, 0.17),
                self.eyeMaterial
            );
            eye.add(sclera);

            var iris = new THREE.Group();
            iris.position.z = 0.018;
            var irisRing = new THREE.Mesh(
                new THREE.RingGeometry(0.105, 0.17, 32),
                self.eyeMaterial
            );
            var pupil = new THREE.Mesh(
                new THREE.CircleGeometry(0.072, 28),
                self.pupilMaterial
            );
            pupil.position.z = 0.008;
            var catchlight = new THREE.Mesh(
                new THREE.CircleGeometry(0.027, 16),
                glowMaterial(0xffffff, 0.88)
            );
            catchlight.position.set(-0.05, 0.055, 0.016);
            iris.add(irisRing, pupil, catchlight);
            eye.add(iris);
            self.eyeRig.add(eye);
            self.eyeGroups.push(eye);
            self.irises.push(iris);
        });
        this.modelRoot.add(this.eyeRig);
    };

    JarvisFace.prototype._buildMouthRig = function () {
        this.mouthRig = new THREE.Group();
        this.mouthRig.position.set(0, -0.58, 1.9);
        this.mouthRig.visible = false;

        var shape = new THREE.Shape();
        shape.absellipse(0, 0, 0.66, 0.15, 0, Math.PI * 2, false, 0);
        this.mouthCavity = new THREE.Mesh(
            new THREE.ShapeGeometry(shape, 28),
            new THREE.MeshBasicMaterial({
                color: 0x010507,
                transparent: true,
                opacity: 0.94,
                depthWrite: false,
                side: THREE.DoubleSide
            })
        );
        this.mouthCavity.scale.y = 0.04;
        this.mouthCavity.renderOrder = 5;

        this.teeth = new THREE.Mesh(
            new THREE.PlaneGeometry(0.76, 0.055),
            new THREE.MeshBasicMaterial({
                color: 0xd8f0ed,
                transparent: true,
                opacity: 0,
                depthWrite: false
            })
        );
        this.teeth.position.set(0, 0.045, 0.012);
        this.teeth.renderOrder = 6;
        this.mouthRig.add(this.mouthCavity, this.teeth);
        this.modelRoot.add(this.mouthRig);
    };

    JarvisFace.prototype._loadHeadModel = function () {
        var self = this;
        var loader = new THREE.GLTFLoader();
        loader.load(this.modelUrl, function (gltf) {
            var sourceMesh = null;
            gltf.scene.traverse(function (object) {
                if (!sourceMesh && object.isMesh) {
                    sourceMesh = object;
                }
            });
            if (!sourceMesh || !sourceMesh.geometry) {
                throw new Error("Head scan contains no mesh");
            }

            var geometry = sourceMesh.geometry.clone();
            geometry.center();
            geometry.computeVertexNormals();
            self.headGeometry = geometry;
            self.basePositions = new Float32Array(
                geometry.attributes.position.array
            );

            self.surfaceMesh = new THREE.Mesh(geometry, self.surfaceMaterial);
            self.wireMesh = new THREE.Mesh(geometry, self.wireMaterial);
            self.pointCloud = new THREE.Points(geometry, self.pointMaterial);
            self.surfaceMesh.renderOrder = 1;
            self.wireMesh.renderOrder = 2;
            self.pointCloud.renderOrder = 3;
            self.modelRoot.add(
                self.surfaceMesh,
                self.wireMesh,
                self.pointCloud
            );

            self.loadingMesh.visible = false;
            self.eyeRig.visible = true;
            self.mouthRig.visible = true;
            self.modelReady = true;
            self.canvas.dataset.faceReady = "true";
        }, undefined, function () {
            self.canvas.dataset.faceReady = "false";
        });
    };

    JarvisFace.prototype.setState = function (state) {
        if (!STATE_STYLE[state]) {
            return;
        }
        this.state = state;
        if (state !== "speaking" && this.speechActive) {
            this.stopSpeaking();
        }
    };

    JarvisFace.prototype.setAudioLevel = function (level) {
        this.audioLevel = clamp(Number(level) || 0, 0, 1);
    };

    JarvisFace.prototype.startSpeaking = function (text, manualTiming) {
        this.speechActive = true;
        this.speechManual = Boolean(manualTiming);
        this.speechText = String(text || "");
        this.speechStarted = this.elapsed;
        this.speechImpulse = 0.34;
        this.speechWidth = 1;
    };

    JarvisFace.prototype.setSpeechCharacter = function (character) {
        var value = String(character || " ");
        var code = value.charCodeAt(0) || 32;
        if (/\s|[，。！？；：,.!?;:]/.test(value)) {
            this.speechImpulse = 0.06;
            this.speechWidth = 1.04;
            return;
        }
        this.speechImpulse = 0.38 + (code % 5) * 0.1;
        this.speechWidth = 0.9 + (code % 4) * 0.065;
    };

    JarvisFace.prototype.stopSpeaking = function () {
        this.speechActive = false;
        this.speechManual = false;
        this.speechText = "";
        this.speechImpulse = 0;
        this.speechWidth = 1;
    };

    JarvisFace.prototype.setWorkspaceOpen = function (isOpen) {
        this.workspaceOpen = Boolean(isOpen);
        this.resize();
    };

    JarvisFace.prototype.resize = function () {
        var width = this.canvas.clientWidth || window.innerWidth;
        var height = this.canvas.clientHeight || window.innerHeight;
        this.renderer.setSize(width, height, false);
        this.camera.aspect = width / Math.max(height, 1);
        this.camera.updateProjectionMatrix();

        if (width <= 700) {
            this.modelRoot.scale.set(0.19, 0.225, 0.21);
            this.layoutX = 0;
            this.layoutY = 0.7;
        } else if (width < 1100) {
            this.modelRoot.scale.set(0.225, 0.265, 0.24);
            this.layoutX = 0.44;
            this.layoutY = 0.04;
        } else if (this.workspaceOpen) {
            this.modelRoot.scale.set(0.205, 0.245, 0.225);
            this.layoutX = 0;
            this.layoutY = 0.04;
        } else {
            this.modelRoot.scale.set(0.245, 0.285, 0.255);
            this.layoutX = 0.38;
            this.layoutY = 0.04;
        }
    };

    JarvisFace.prototype._blinkAmount = function () {
        if (this.elapsed >= this.nextBlink && this.blinkStarted < 0) {
            this.blinkStarted = this.elapsed;
        }
        if (this.blinkStarted < 0) {
            return 1;
        }
        var progress = (this.elapsed - this.blinkStarted) / 0.17;
        if (progress >= 1) {
            this.blinkStarted = -1;
            this.nextBlink = this.elapsed + 2.7 + Math.random() * 3.7;
            return 1;
        }
        return 0.08 + Math.abs(progress - 0.5) * 1.84;
    };

    JarvisFace.prototype._speechShape = function () {
        if (this.state !== "speaking" || !this.speechActive) {
            return { open: 0, width: 1 };
        }
        if (this.speechManual) {
            return { open: this.speechImpulse, width: this.speechWidth };
        }
        if (!this.speechText) {
            return {
                open: 0.34 + Math.abs(Math.sin(this.elapsed * 8.4)) * 0.42,
                width: 1
            };
        }
        var progress = Math.max(0, this.elapsed - this.speechStarted);
        var character = this.speechText.charAt(
            Math.floor(progress * 8.5) % this.speechText.length
        );
        var code = character.charCodeAt(0) || 32;
        if (/\s|[，。！？；：,.!?;:]/.test(character)) {
            return { open: 0.06, width: 1.04 };
        }
        return {
            open: 0.38 + (code % 5) * 0.1,
            width: 0.9 + (code % 4) * 0.065
        };
    };

    JarvisFace.prototype._deformMouth = function (openAmount, widthAmount) {
        if (!this.headGeometry || !this.basePositions) {
            return;
        }
        var attribute = this.headGeometry.attributes.position;
        var array = attribute.array;
        var base = this.basePositions;
        var index;
        for (index = 0; index < attribute.count; index += 1) {
            var offset = index * 3;
            var x = base[offset];
            var y = base[offset + 1];
            var z = base[offset + 2];
            var frontWeight = smoothstep(0.95, 1.75, z);
            var mouthX = 1 - smoothstep(0.62, 1.34, Math.abs(x));
            var mouthY = 1 - smoothstep(0.14, 0.38, Math.abs(y + 0.58));
            var mouthWeight = frontWeight * mouthX * mouthY;
            var lowerLip = smoothstep(-0.57, -0.82, y);
            var upperLip = 1 - lowerLip;
            var jawWeight = frontWeight
                * smoothstep(-0.5, -2.45, y)
                * (1 - smoothstep(1.35, 2.7, Math.abs(x)));

            array[offset] = x * (1 + (widthAmount - 1) * mouthWeight * 0.4);
            array[offset + 1] = y
                - openAmount * mouthWeight * lowerLip * 0.34
                + openAmount * mouthWeight * upperLip * 0.075
                - openAmount * jawWeight * 0.13;
            array[offset + 2] = z + openAmount * mouthWeight * 0.035;
        }
        attribute.needsUpdate = true;
    };

    JarvisFace.prototype._render = function () {
        if (document.hidden) {
            this.clock.getDelta();
            return;
        }
        var delta = Math.min(this.clock.getDelta(), 0.05);
        this.frameAccumulator += delta;
        var interval = this.reducedMotion
            ? 1 / 24
            : (this.state === "idle" ? 1 / 30 : 1 / 60);
        if (this.frameAccumulator < interval) {
            return;
        }
        delta = Math.min(this.frameAccumulator, 0.125);
        this.frameAccumulator = 0;
        this.elapsed += delta;
        var style = STATE_STYLE[this.state];
        var lerp = 1 - Math.pow(0.001, delta);
        var targetColor = new THREE.Color(style.color);

        this.wireMaterial.color.lerp(targetColor, lerp * 0.6);
        this.pointMaterial.color.lerp(targetColor, lerp * 0.46);
        this.eyeMaterial.color.lerp(targetColor, lerp * 0.5);
        this.surfaceMaterial.color.lerp(targetColor, lerp * 0.2);
        this.wireMaterial.opacity += (
            style.opacity - this.wireMaterial.opacity
        ) * lerp;

        var blink = this._blinkAmount();
        this.eyeGroups.forEach(function (eye) {
            eye.scale.y += (style.eye * blink - eye.scale.y) * lerp;
        });
        var gazeX = style.gazeX + this.pointer.x * 0.05;
        var gazeY = style.gazeY + this.pointer.y * 0.035;
        this.irises.forEach(function (iris) {
            iris.position.x += (gazeX - iris.position.x) * lerp;
            iris.position.y += (gazeY - iris.position.y) * lerp;
        });

        if (this.speechManual) {
            this.speechImpulse *= Math.pow(0.2, delta);
        }
        var speech = this._speechShape();
        var targetOpen = this.reducedMotion ? speech.open * 0.62 : speech.open;
        this.currentMouthOpen += (
            targetOpen - this.currentMouthOpen
        ) * Math.min(1, delta * 18);
        this._deformMouth(this.currentMouthOpen, speech.width);
        this.mouthCavity.scale.y += (
            0.04 + this.currentMouthOpen * 1.8 - this.mouthCavity.scale.y
        ) * Math.min(1, delta * 20);
        this.mouthCavity.scale.x += (
            speech.width - this.mouthCavity.scale.x
        ) * lerp;
        this.teeth.material.opacity += (
            (this.currentMouthOpen > 0.38 ? 0.3 : 0) - this.teeth.material.opacity
        ) * lerp;
        this.canvas.dataset.mouthOpen = this.currentMouthOpen.toFixed(3);

        var drift = this.reducedMotion
            ? 0
            : Math.sin(this.elapsed * 0.48) * 0.018;
        this.faceRoot.rotation.y += (
            this.pointer.x * 0.065 + drift - this.faceRoot.rotation.y
        ) * lerp;
        this.faceRoot.rotation.x += (
            -this.pointer.y * 0.035 - this.faceRoot.rotation.x
        ) * lerp;
        this.faceRoot.rotation.z += (
            style.tilt - this.faceRoot.rotation.z
        ) * lerp;
        this.faceRoot.position.x += (
            this.layoutX - this.faceRoot.position.x
        ) * lerp;
        this.faceRoot.position.y += (
            this.layoutY - this.faceRoot.position.y
        ) * lerp;

        this.smoothedAudio += (
            this.audioLevel - this.smoothedAudio
        ) * Math.min(1, delta * 12);
        if (this.state === "listening") {
            this.pointMaterial.opacity = 0.055 + this.smoothedAudio * 0.1;
        } else {
            this.pointMaterial.opacity += (
                0.055 - this.pointMaterial.opacity
            ) * lerp;
        }
        if (this.state === "error" && !this.reducedMotion) {
            this.faceRoot.position.x += (Math.random() - 0.5) * 0.003;
        }

        this.renderer.render(this.scene, this.camera);
    };

    JarvisFace.prototype.getDiagnostics = function () {
        return {
            modelReady: this.modelReady,
            mouthOpen: this.currentMouthOpen,
            state: this.state
        };
    };

    JarvisFace.prototype.destroy = function () {
        this.renderer.setAnimationLoop(null);
        window.removeEventListener("resize", this._resizeHandler);
        window.removeEventListener("pointermove", this._pointerHandler);
        var geometries = new Set();
        var materials = new Set();
        this.scene.traverse(function (object) {
            if (object.geometry) {
                geometries.add(object.geometry);
            }
            if (Array.isArray(object.material)) {
                object.material.forEach(function (item) { materials.add(item); });
            } else if (object.material) {
                materials.add(object.material);
            }
        });
        geometries.forEach(function (geometry) { geometry.dispose(); });
        materials.forEach(function (item) { item.dispose(); });
        this.renderer.dispose();
    };

    window.JarvisFace = JarvisFace;
}());
