(function () {
    "use strict";

    var STATE_STYLE = {
        idle: { color: 0x66ddd8, tilt: 0, opacity: 0.1 },
        listening: { color: 0x9ce8bd, tilt: -0.018, opacity: 0.15 },
        thinking: { color: 0xffca61, tilt: 0.014, opacity: 0.13 },
        speaking: { color: 0x66ddd8, tilt: 0, opacity: 0.17 },
        executing: { color: 0x9ce8bd, tilt: 0, opacity: 0.15 },
        error: { color: 0xff7373, tilt: 0, opacity: 0.19 }
    };

    function clamp(value, minimum, maximum) {
        return Math.max(minimum, Math.min(maximum, value));
    }

    function smoothstep(minimum, maximum, value) {
        var normalized = clamp((value - minimum) / (maximum - minimum), 0, 1);
        return normalized * normalized * (3 - 2 * normalized);
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
        this.mouthVelocity = 0;
        this.pointer = { x: 0, y: 0 };
        this.clock = new THREE.Clock();
        this.elapsed = 0;
        this.frameAccumulator = 0;
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
            opacity: 0.1,
            blending: THREE.AdditiveBlending,
            depthWrite: false,
            depthTest: true
        });
        this.surfaceMaterial = new THREE.MeshPhongMaterial({
            color: 0x102326,
            emissive: 0x02090b,
            specular: 0x78a7a5,
            shininess: 46,
            transparent: false,
            depthWrite: true,
            depthTest: true,
            side: THREE.FrontSide,
            polygonOffset: true,
            polygonOffsetFactor: 1,
            polygonOffsetUnits: 1
        });
        this.edgeMaterial = new THREE.LineBasicMaterial({
            color: 0x9ce8e3,
            transparent: true,
            opacity: 0.3,
            blending: THREE.AdditiveBlending,
            depthWrite: false,
            depthTest: true
        });
        this._buildLoadingMesh();
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

    JarvisFace.prototype._buildMouthRig = function () {
        this.mouthRig = new THREE.Group();
        this.mouthRig.position.set(0, -0.25, 2.23);
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
                depthTest: false,
                side: THREE.DoubleSide
            })
        );
        this.mouthCavity.scale.y = 0.04;
        this.mouthCavity.renderOrder = 5;

        var lipShape = new THREE.Shape();
        lipShape.absellipse(0, 0, 0.72, 0.19, 0, Math.PI * 2, false, 0);
        var lipHole = new THREE.Path();
        lipHole.absellipse(0, 0, 0.64, 0.12, 0, Math.PI * 2, true, 0);
        lipShape.holes.push(lipHole);
        this.lipRim = new THREE.Mesh(
            new THREE.ShapeGeometry(lipShape, 28),
            new THREE.MeshBasicMaterial({
                color: 0x66ddd8,
                transparent: true,
                opacity: 0.28,
                blending: THREE.AdditiveBlending,
                depthWrite: false,
                depthTest: false,
                side: THREE.DoubleSide
            })
        );
        this.lipRim.position.z = 0.014;
        this.lipRim.scale.y = 0.04;
        this.lipRim.renderOrder = 6;
        this.mouthRig.add(this.mouthCavity, this.lipRim);
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
            self.edgeLines = new THREE.LineSegments(
                new THREE.EdgesGeometry(geometry, 12),
                self.edgeMaterial
            );
            self.surfaceMesh.renderOrder = 1;
            self.wireMesh.renderOrder = 2;
            self.edgeLines.renderOrder = 3;
            self.modelRoot.add(
                self.surfaceMesh,
                self.wireMesh,
                self.edgeLines
            );

            self.loadingMesh.visible = false;
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
        this.speechImpulse = 0.2;
        this.speechWidth = 1;
    };

    JarvisFace.prototype.setSpeechCharacter = function (character) {
        var value = String(character || " ");
        var code = value.charCodeAt(0) || 32;
        if (/\s|[，。！？；：,.!?;:]/.test(value)) {
            this.speechImpulse = 0.015;
            this.speechWidth = 1.02;
            return;
        }
        this.speechImpulse = 0.18 + (code % 4) * 0.065;
        this.speechWidth = 0.94 + (code % 3) * 0.04;
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

    JarvisFace.prototype._speechShape = function () {
        if (this.state !== "speaking" || !this.speechActive) {
            return { open: 0, width: 1 };
        }
        if (this.speechManual) {
            return { open: this.speechImpulse, width: this.speechWidth };
        }
        if (!this.speechText) {
            return {
                open: 0.18 + Math.abs(Math.sin(this.elapsed * 6.8)) * 0.2,
                width: 1
            };
        }
        var progress = Math.max(0, this.elapsed - this.speechStarted);
        var character = this.speechText.charAt(
            Math.floor(progress * 8.5) % this.speechText.length
        );
        var code = character.charCodeAt(0) || 32;
        if (/\s|[，。！？；：,.!?;:]/.test(character)) {
            return { open: 0.015, width: 1.02 };
        }
        return {
            open: 0.18 + (code % 4) * 0.065,
            width: 0.94 + (code % 3) * 0.04
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
            var frontWeight = smoothstep(1.25, 2.08, z);
            var mouthX = 1 - smoothstep(0.62, 1.34, Math.abs(x));
            var mouthY = 1 - smoothstep(0.1, 0.3, Math.abs(y + 0.25));
            var mouthWeight = frontWeight * mouthX * mouthY;
            var lowerLip = smoothstep(-0.24, -0.48, y);
            var upperLip = 1 - lowerLip;
            var jawWeight = frontWeight
                * smoothstep(-0.18, -1.75, y)
                * (1 - smoothstep(1.35, 2.7, Math.abs(x)));

            array[offset] = x * (1 + (widthAmount - 1) * mouthWeight * 0.22);
            array[offset + 1] = y
                - openAmount * mouthWeight * lowerLip * 0.15
                + openAmount * mouthWeight * upperLip * 0.025
                - openAmount * jawWeight * 0.05;
            array[offset + 2] = z + openAmount * mouthWeight * 0.012;
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
        var surfaceColor = targetColor.clone().multiplyScalar(0.12);

        this.wireMaterial.color.lerp(targetColor, lerp * 0.6);
        this.edgeMaterial.color.lerp(targetColor, lerp * 0.48);
        this.lipRim.material.color.lerp(targetColor, lerp * 0.5);
        this.surfaceMaterial.color.lerp(surfaceColor, lerp * 0.16);
        this.wireMaterial.opacity += (
            style.opacity - this.wireMaterial.opacity
        ) * lerp;
        this.edgeMaterial.opacity += (
            0.25 + style.opacity * 0.42 - this.edgeMaterial.opacity
        ) * lerp;

        if (this.speechManual) {
            this.speechImpulse *= Math.pow(0.12, delta);
        }
        var speech = this._speechShape();
        var targetOpen = this.reducedMotion ? speech.open * 0.62 : speech.open;
        this.mouthVelocity += (
            targetOpen - this.currentMouthOpen
        ) * 72 * delta;
        this.mouthVelocity *= Math.exp(-13 * delta);
        this.currentMouthOpen = clamp(
            this.currentMouthOpen + this.mouthVelocity * delta,
            0,
            0.52
        );
        this._deformMouth(this.currentMouthOpen, speech.width);
        this.mouthCavity.scale.y += (
            0.04 + this.currentMouthOpen * 3.2 - this.mouthCavity.scale.y
        ) * Math.min(1, delta * 14);
        this.mouthCavity.scale.x += (
            speech.width - this.mouthCavity.scale.x
        ) * lerp;
        this.lipRim.scale.y += (
            0.04 + this.currentMouthOpen * 3.2 - this.lipRim.scale.y
        ) * Math.min(1, delta * 14);
        this.lipRim.scale.x += (
            speech.width - this.lipRim.scale.x
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
