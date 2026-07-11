(function () {
    "use strict";

    var STATE_STYLE = {
        idle: { color: 0x66ddd8, tilt: 0, opacity: 0.18 },
        listening: { color: 0x9ce8bd, tilt: -0.012, opacity: 0.27 },
        thinking: { color: 0xffca61, tilt: 0.012, opacity: 0.23 },
        speaking: { color: 0x66ddd8, tilt: 0, opacity: 0.3 },
        executing: { color: 0x9ce8bd, tilt: 0, opacity: 0.25 },
        error: { color: 0xff7373, tilt: 0, opacity: 0.32 }
    };

    // Azure Speech viseme IDs. These are phoneme groups emitted by the TTS
    // service, not guesses derived from response text.
    var VISEME_SHAPES = {
        0: { open: 0.02, width: 1.02 }, // silence
        1: { open: 0.72, width: 1.02 }, // ae, ax, ah
        2: { open: 0.9, width: 0.96 },  // aa
        3: { open: 0.72, width: 0.78 }, // ao
        4: { open: 0.56, width: 1.05 }, // ey, eh, uh
        5: { open: 0.42, width: 0.82 }, // er
        6: { open: 0.3, width: 1.24 },  // y, iy, ih, ix
        7: { open: 0.34, width: 0.58 }, // w, uw
        8: { open: 0.48, width: 0.64 }, // ow
        9: { open: 0.75, width: 0.84 }, // aw
        10: { open: 0.62, width: 0.75 }, // oy
        11: { open: 0.62, width: 1.02 }, // ay
        12: { open: 0.4, width: 0.98 }, // h
        13: { open: 0.35, width: 0.86 }, // r
        14: { open: 0.38, width: 1.08 }, // l
        15: { open: 0.2, width: 1.16 },  // s, z
        16: { open: 0.32, width: 0.82 }, // sh, ch, jh, zh
        17: { open: 0.22, width: 1.04 }, // th, dh
        18: { open: 0.14, width: 1.1 },  // f, v
        19: { open: 0.28, width: 1.02 }, // d, t, n
        20: { open: 0.38, width: 0.92 }, // k, g, ng
        21: { open: 0.04, width: 0.92 }  // p, b, m
    };

    function clamp(value, minimum, maximum) {
        return Math.max(minimum, Math.min(maximum, value));
    }

    function makeMaterial(color, opacity) {
        return new THREE.MeshBasicMaterial({
            color: color,
            transparent: opacity < 1,
            opacity: opacity,
            depthTest: true,
            depthWrite: opacity >= 1,
            side: THREE.DoubleSide
        });
    }

    function makeEye(x, accentMaterial, darkMaterial) {
        var group = new THREE.Group();
        group.position.set(x, 0.2, 0.654);

        var socket = new THREE.Mesh(
            new THREE.CircleGeometry(0.12, 32), darkMaterial
        );
        socket.scale.set(1.42, 0.58, 1);

        var iris = new THREE.Mesh(
            new THREE.RingGeometry(0.041, 0.069, 28), accentMaterial
        );
        iris.position.z = 0.004;
        var pupil = new THREE.Mesh(
            new THREE.CircleGeometry(0.021, 24), accentMaterial
        );
        pupil.position.z = 0.006;
        group.add(socket, iris, pupil);
        group.userData.iris = iris;
        group.userData.pupil = pupil;
        return group;
    }

    function makeBrow(x, mirror, material) {
        var start = new THREE.Vector3(x - 0.13 * mirror, 0.37, 0.642);
        var middle = new THREE.Vector3(x, 0.405, 0.658);
        var end = new THREE.Vector3(x + 0.14 * mirror, 0.37, 0.642);
        var curve = new THREE.QuadraticBezierCurve3(start, middle, end);
        return new THREE.Mesh(
            new THREE.TubeGeometry(curve, 14, 0.009, 6, false), material
        );
    }

    function JarvisFace(canvas) {
        if (!window.THREE) {
            throw new Error("Three.js is unavailable");
        }
        this.canvas = canvas;
        this.reducedMotion = window.matchMedia(
            "(prefers-reduced-motion: reduce)"
        ).matches;
        this.state = "idle";
        this.audioLevel = 0;
        this.smoothedAudio = 0;
        this.speechActive = false;
        this.currentViseme = 0;
        this.targetMouth = VISEME_SHAPES[0];
        this.currentMouthOpen = 0.02;
        this.currentMouthWidth = 1.02;
        this.pointer = { x: 0, y: 0 };
        this.clock = new THREE.Clock();
        this.elapsed = 0;
        this.frameAccumulator = 0;
        this.nextBlinkAt = 2.4;
        this.blinkStartedAt = -1;
        this.workspaceOpen = false;
        this.layoutX = 0;
        this.layoutY = 0;

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

        this.scene.add(new THREE.AmbientLight(0x79a5a5, 0.65));
        var keyLight = new THREE.PointLight(0xc7ffff, 0.95, 10);
        keyLight.position.set(-2.1, 1.7, 3.6);
        this.scene.add(keyLight);
        var rimLight = new THREE.PointLight(0xffd37d, 0.3, 8);
        rimLight.position.set(2.3, -0.5, 2.6);
        this.scene.add(rimLight);

        this._buildFace();
        this.resize();
        this.canvas.dataset.faceReady = "true";

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

    JarvisFace.prototype._buildFace = function () {
        var geometry = new THREE.IcosahedronGeometry(1, 3);
        var positions = geometry.attributes.position;
        for (var index = 0; index < positions.count; index += 1) {
            var y = positions.getY(index);
            var jawTaper = y < -0.12 ? 1 - Math.min(0.2, (-y - 0.12) * 0.2) : 1;
            positions.setXYZ(
                index,
                positions.getX(index) * 0.76 * jawTaper,
                y,
                positions.getZ(index) * 0.68
            );
        }
        positions.needsUpdate = true;
        geometry.computeVertexNormals();

        this.surfaceMaterial = new THREE.MeshPhongMaterial({
            color: 0x0a1a1d,
            emissive: 0x010607,
            specular: 0x568b89,
            shininess: 34,
            depthTest: true,
            depthWrite: true,
            side: THREE.FrontSide,
            polygonOffset: true,
            polygonOffsetFactor: 1,
            polygonOffsetUnits: 1
        });
        this.wireMaterial = new THREE.MeshBasicMaterial({
            color: 0x66ddd8,
            wireframe: true,
            transparent: true,
            opacity: STATE_STYLE.idle.opacity,
            blending: THREE.AdditiveBlending,
            depthTest: true,
            depthWrite: false
        });
        this.featureMaterial = makeMaterial(0x77e3df, 0.94);
        this.eyeMaterial = makeMaterial(0xa7f2d2, 1);
        this.darkMaterial = makeMaterial(0x010506, 1);

        this.surfaceMesh = new THREE.Mesh(geometry, this.surfaceMaterial);
        this.wireMesh = new THREE.Mesh(geometry, this.wireMaterial);
        this.surfaceMesh.renderOrder = 1;
        this.wireMesh.renderOrder = 2;
        this.modelRoot.add(this.surfaceMesh, this.wireMesh);

        this.leftEye = makeEye(-0.24, this.eyeMaterial, this.darkMaterial);
        this.rightEye = makeEye(0.24, this.eyeMaterial, this.darkMaterial);
        this.leftBrow = makeBrow(-0.24, 1, this.featureMaterial);
        this.rightBrow = makeBrow(0.24, -1, this.featureMaterial);
        this.modelRoot.add(
            this.leftEye, this.rightEye, this.leftBrow, this.rightBrow
        );

        var noseGeometry = new THREE.BufferGeometry().setFromPoints([
            new THREE.Vector3(0, 0.08, 0.682),
            new THREE.Vector3(-0.035, -0.08, 0.674),
            new THREE.Vector3(0.04, -0.08, 0.674)
        ]);
        this.nose = new THREE.Line(
            noseGeometry,
            new THREE.LineBasicMaterial({
                color: 0x66aaa7,
                transparent: true,
                opacity: 0.58,
                depthTest: true
            })
        );
        this.modelRoot.add(this.nose);

        this.mouthRig = new THREE.Group();
        this.mouthRig.position.set(0, -0.285, 0.665);
        this.mouthRig.rotation.x = 0.19;
        this.mouthCavity = new THREE.Mesh(
            new THREE.CircleGeometry(0.115, 36), this.darkMaterial
        );
        this.mouthRim = new THREE.Mesh(
            new THREE.RingGeometry(0.112, 0.128, 36), this.featureMaterial
        );
        this.mouthRim.position.z = 0.004;
        this.mouthRig.add(this.mouthCavity, this.mouthRim);
        this.modelRoot.add(this.mouthRig);

        var templeMaterial = new THREE.MeshBasicMaterial({
            color: 0x66ddd8,
            transparent: true,
            opacity: 0.28,
            depthTest: true,
            depthWrite: false,
            side: THREE.DoubleSide
        });
        [-1, 1].forEach(function (side) {
            var temple = new THREE.Mesh(
                new THREE.RingGeometry(0.07, 0.083, 24), templeMaterial
            );
            temple.position.set(side * 0.735, 0.02, 0);
            temple.rotation.y = Math.PI / 2;
            this.modelRoot.add(temple);
        }, this);
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

    JarvisFace.prototype.startSpeaking = function () {
        this.speechActive = true;
        this.setViseme(0);
    };

    JarvisFace.prototype.setViseme = function (visemeId) {
        var normalized = Number(visemeId);
        if (!Number.isInteger(normalized) || !VISEME_SHAPES[normalized]) {
            normalized = 0;
        }
        this.currentViseme = normalized;
        this.targetMouth = VISEME_SHAPES[normalized];
    };

    JarvisFace.prototype.stopSpeaking = function () {
        this.speechActive = false;
        this.setViseme(0);
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
            this.modelRoot.scale.setScalar(0.5);
            this.layoutX = 0;
            this.layoutY = 0.72;
        } else if (width < 1100) {
            this.modelRoot.scale.setScalar(0.82);
            this.layoutX = 0.42;
            this.layoutY = 0.05;
        } else if (this.workspaceOpen) {
            this.modelRoot.scale.setScalar(0.78);
            this.layoutX = 0;
            this.layoutY = 0.04;
        } else {
            this.modelRoot.scale.setScalar(0.92);
            this.layoutX = 0.42;
            this.layoutY = 0.04;
        }
    };

    JarvisFace.prototype._blinkScale = function () {
        if (this.reducedMotion) {
            return 1;
        }
        if (this.blinkStartedAt < 0 && this.elapsed >= this.nextBlinkAt) {
            this.blinkStartedAt = this.elapsed;
        }
        if (this.blinkStartedAt < 0) {
            return 1;
        }
        var progress = (this.elapsed - this.blinkStartedAt) / 0.17;
        if (progress >= 1) {
            this.blinkStartedAt = -1;
            this.nextBlinkAt = this.elapsed + 2.6 + Math.random() * 2.4;
            return 1;
        }
        return 1 - Math.sin(progress * Math.PI) * 0.92;
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
        var surfaceColor = targetColor.clone().multiplyScalar(0.105);
        this.wireMaterial.color.lerp(targetColor, lerp * 0.6);
        this.featureMaterial.color.lerp(targetColor, lerp * 0.5);
        this.surfaceMaterial.color.lerp(surfaceColor, lerp * 0.16);
        this.wireMaterial.opacity += (
            style.opacity - this.wireMaterial.opacity
        ) * lerp;

        var mouth = this.speechActive ? this.targetMouth : VISEME_SHAPES[0];
        this.currentMouthOpen += (
            mouth.open - this.currentMouthOpen
        ) * Math.min(1, delta * 22);
        this.currentMouthWidth += (
            mouth.width - this.currentMouthWidth
        ) * Math.min(1, delta * 18);
        var mouthWidth = 0.95 * this.currentMouthWidth;
        var mouthHeight = 0.07 + this.currentMouthOpen * 1.15;
        this.mouthCavity.scale.set(mouthWidth, mouthHeight, 1);
        this.mouthRim.scale.set(mouthWidth, mouthHeight, 1);
        this.mouthRig.position.y = -0.285 - this.currentMouthOpen * 0.025;
        this.canvas.dataset.mouthOpen = this.currentMouthOpen.toFixed(3);
        this.canvas.dataset.viseme = String(this.currentViseme);

        var blinkScale = this._blinkScale();
        var eyeStateScale = this.state === "listening" ? 1.08 : 1;
        this.leftEye.scale.y = blinkScale * eyeStateScale;
        this.rightEye.scale.y = blinkScale * eyeStateScale;
        this.smoothedAudio += (
            this.audioLevel - this.smoothedAudio
        ) * Math.min(1, delta * 12);
        var irisScale = 1 + this.smoothedAudio * 0.18;
        [this.leftEye, this.rightEye].forEach(function (eye, index) {
            var direction = index === 0 ? -1 : 1;
            var gazeX = this.pointer.x * 0.015;
            var gazeY = this.pointer.y * 0.01;
            if (this.state === "thinking") {
                gazeX += direction * 0.004;
                gazeY += 0.018;
            }
            eye.userData.iris.position.x += (
                gazeX - eye.userData.iris.position.x
            ) * lerp;
            eye.userData.iris.position.y += (
                gazeY - eye.userData.iris.position.y
            ) * lerp;
            eye.userData.pupil.position.x = eye.userData.iris.position.x;
            eye.userData.pupil.position.y = eye.userData.iris.position.y;
            eye.userData.iris.scale.setScalar(irisScale);
        }, this);

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
        if (this.state === "error" && !this.reducedMotion) {
            this.faceRoot.position.x += (Math.random() - 0.5) * 0.003;
        }

        this.renderer.render(this.scene, this.camera);
    };

    JarvisFace.prototype.getDiagnostics = function () {
        return {
            modelReady: true,
            mouthOpen: this.currentMouthOpen,
            viseme: this.currentViseme,
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
        materials.forEach(function (material) { material.dispose(); });
        this.renderer.dispose();
    };

    window.JarvisFace = JarvisFace;
}());
