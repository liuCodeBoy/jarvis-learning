(function () {
    "use strict";

    var STATE_STYLE = {
        idle: { color: 0x66ddd8, tilt: 0, opacity: 0.24, signal: 0.48 },
        listening: { color: 0x9ce8bd, tilt: -0.012, opacity: 0.42, signal: 0.82 },
        thinking: { color: 0xffca61, tilt: 0.012, opacity: 0.38, signal: 0.72 },
        speaking: { color: 0x66ddd8, tilt: 0, opacity: 0.4, signal: 0.78 },
        executing: { color: 0x9ce8bd, tilt: 0, opacity: 0.34, signal: 0.65 },
        error: { color: 0xff7373, tilt: 0, opacity: 0.54, signal: 0.9 }
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

    function makeShape(points) {
        var shape = new THREE.Shape();
        shape.moveTo(points[0][0], points[0][1]);
        points.slice(1).forEach(function (point) {
            shape.lineTo(point[0], point[1]);
        });
        shape.closePath();
        return shape;
    }

    function mirrorPoints(points) {
        return points.map(function (point) {
            return [-point[0], point[1]];
        }).reverse();
    }

    function makePlate(points, material, edgeMaterial, options) {
        var settings = options || {};
        var depth = settings.depth || 0.05;
        var geometry = new THREE.ExtrudeGeometry(makeShape(points), {
            depth: depth,
            bevelEnabled: settings.bevel !== false,
            bevelSegments: 1,
            bevelSize: settings.bevelSize || 0.008,
            bevelThickness: settings.bevelThickness || 0.008,
            curveSegments: 1,
            steps: 1
        });
        geometry.translate(0, 0, -depth / 2);
        geometry.computeVertexNormals();

        var group = new THREE.Group();
        group.position.z = settings.z || 0;
        var surface = new THREE.Mesh(geometry, material);
        surface.renderOrder = settings.renderOrder || 1;
        group.add(surface);
        if (edgeMaterial) {
            var outline = new THREE.LineSegments(
                new THREE.EdgesGeometry(geometry, 52), edgeMaterial
            );
            outline.renderOrder = 2;
            group.add(outline);
        }
        return group;
    }

    function makeTube(points, radius, material) {
        var vectors = points.map(function (point) {
            return new THREE.Vector3(point[0], point[1], point[2] || 0);
        });
        var curve = vectors.length === 2
            ? new THREE.LineCurve3(vectors[0], vectors[1])
            : new THREE.CatmullRomCurve3(vectors, false, "centripetal");
        var mesh = new THREE.Mesh(
            new THREE.TubeGeometry(curve, 18, radius, 5, false), material
        );
        mesh.renderOrder = 4;
        return mesh;
    }

    function makeEye(side, socketMaterial, accentMaterial, coreMaterial) {
        var group = new THREE.Group();
        group.position.set(side * 0.265, 0.155, 0.245);
        group.scale.x = side;

        var socketPoints = [
            [-0.145, 0], [-0.09, 0.04], [0.09, 0.032],
            [0.15, 0], [0.08, -0.038], [-0.1, -0.032]
        ];
        var socket = new THREE.Mesh(
            new THREE.ShapeGeometry(makeShape(socketPoints)), socketMaterial
        );
        socket.renderOrder = 3;

        var aperture = new THREE.Group();
        aperture.add(
            makeTube([
                [-0.132, 0.003, 0.006], [-0.04, 0.032, 0.008],
                [0.09, 0.025, 0.007], [0.136, 0.002, 0.006]
            ], 0.0024, accentMaterial),
            makeTube([
                [-0.128, -0.003, 0.006], [-0.04, -0.026, 0.008],
                [0.08, -0.025, 0.007], [0.132, -0.002, 0.006]
            ], 0.0015, accentMaterial)
        );

        var gaze = new THREE.Group();
        var iris = new THREE.Mesh(
            new THREE.RingGeometry(0.017, 0.026, 24), accentMaterial
        );
        iris.position.z = 0.009;
        iris.renderOrder = 5;
        var eyeCore = new THREE.Mesh(
            new THREE.CircleGeometry(0.006, 16), coreMaterial
        );
        eyeCore.position.z = 0.011;
        eyeCore.renderOrder = 5;
        gaze.add(iris, eyeCore);
        aperture.add(gaze);
        group.add(socket, aperture);
        group.userData.aperture = aperture;
        group.userData.gaze = gaze;
        group.userData.side = side;
        return group;
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
        this.baseYaw = -0.07;

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
        this.faceRoot.rotation.y = this.baseYaw;
        this.modelRoot = new THREE.Group();
        this.faceRoot.add(this.modelRoot);
        this.scene.add(this.faceRoot);

        this.scene.add(new THREE.AmbientLight(0x7f9193, 0.34));
        var keyLight = new THREE.PointLight(0xe2ffff, 0.88, 10);
        keyLight.position.set(-2.2, 2.1, 3.8);
        this.scene.add(keyLight);
        var fillLight = new THREE.PointLight(0x5aa8ad, 0.32, 8);
        fillLight.position.set(2.2, 0.3, 3.1);
        this.scene.add(fillLight);
        var rimLight = new THREE.PointLight(0xffc46b, 0.38, 8);
        rimLight.position.set(2.4, -0.8, 1.1);
        this.scene.add(rimLight);

        this._buildFace();
        this.resize();
        this.canvas.dataset.faceReady = "true";
        this.canvas.dataset.faceDesign = "segmented-mask";
        this.canvas.dataset.mouthMechanism = "articulated-plates";

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
        function makePanelMaterial(color, specular, shininess) {
            var material = new THREE.MeshPhongMaterial({
                color: color,
                emissive: 0x010304,
                specular: specular,
                shininess: shininess,
                flatShading: true,
                depthTest: true,
                depthWrite: true,
                side: THREE.DoubleSide
            });
            material.userData.baseColor = new THREE.Color(color);
            return material;
        }

        this.coreMaterial = makePanelMaterial(0x05090b, 0x172124, 26);
        this.panelMaterial = makePanelMaterial(0x10191c, 0x66858a, 58);
        this.panelDarkMaterial = makePanelMaterial(0x0a1114, 0x40575b, 45);
        this.ridgeMaterial = makePanelMaterial(0x172225, 0x78999d, 66);
        this.panelMaterials = [
            this.coreMaterial, this.panelMaterial,
            this.panelDarkMaterial, this.ridgeMaterial
        ];
        this.edgeMaterial = new THREE.LineBasicMaterial({
            color: STATE_STYLE.idle.color,
            transparent: true,
            opacity: STATE_STYLE.idle.opacity,
            blending: THREE.AdditiveBlending,
            depthTest: true,
            depthWrite: false
        });
        this.signalMaterial = makeMaterial(
            STATE_STYLE.idle.color, STATE_STYLE.idle.signal
        );
        this.signalMaterial.blending = THREE.AdditiveBlending;
        this.eyeMaterial = makeMaterial(0x8ce4ce, 0.9);
        this.eyeMaterial.blending = THREE.AdditiveBlending;
        this.eyeCoreMaterial = makeMaterial(0xe7ffff, 1);
        this.darkMaterial = makeMaterial(0x010405, 1);

        var corePoints = [
            [-0.18, 0.9], [0.18, 0.9], [0.42, 0.77],
            [0.56, 0.56], [0.64, 0.31], [0.67, 0.02],
            [0.61, -0.29], [0.47, -0.57], [0.25, -0.78],
            [0.08, -0.86], [-0.08, -0.86], [-0.25, -0.78],
            [-0.47, -0.57], [-0.61, -0.29], [-0.67, 0.02],
            [-0.64, 0.31], [-0.56, 0.56], [-0.42, 0.77]
        ];
        this.modelRoot.add(makePlate(
            corePoints, this.coreMaterial, null,
            {
                depth: 0.2,
                z: 0.015,
                bevelSize: 0.018,
                bevelThickness: 0.016
            }
        ));

        var upperFaceRig = new THREE.Group();
        this.lowerJawRig = new THREE.Group();
        this.lowerJawRig.position.y = -0.4;
        this.lowerJawRig.userData.closedPosition = this.lowerJawRig.position.clone();
        var lowerJawPanels = new THREE.Group();
        lowerJawPanels.position.y = 0.4;
        this.lowerJawRig.add(lowerJawPanels);
        this.modelRoot.add(upperFaceRig, this.lowerJawRig);
        var self = this;
        function addPlate(parent, points, material, options) {
            var plate = makePlate(
                points, material, self.edgeMaterial, options
            );
            parent.add(plate);
            return plate;
        }
        function addMirrored(parent, points, material, options) {
            addPlate(parent, points, material, options);
            addPlate(parent, mirrorPoints(points), material, options);
        }
        function addArticulatedPair(
            parent, points, pivot, material, options
        ) {
            function addRig(platePoints, platePivot, side) {
                var rig = new THREE.Group();
                rig.position.set(platePivot[0], platePivot[1], 0);
                rig.userData.closedPosition = rig.position.clone();
                rig.userData.side = side;
                var localPoints = platePoints.map(function (point) {
                    return [
                        point[0] - platePivot[0],
                        point[1] - platePivot[1]
                    ];
                });
                addPlate(rig, localPoints, material, options);
                parent.add(rig);
                return rig;
            }

            return [
                addRig(
                    mirrorPoints(points), [-pivot[0], pivot[1]], -1
                ),
                addRig(points, pivot, 1)
            ];
        }

        addPlate(upperFaceRig, [
            [-0.13, 0.84], [0.13, 0.84], [0.28, 0.7],
            [0.23, 0.49], [0.1, 0.37], [-0.1, 0.37],
            [-0.23, 0.49], [-0.28, 0.7]
        ], this.panelMaterial, { depth: 0.06, z: 0.17 });
        addMirrored(upperFaceRig, [
            [0.17, 0.82], [0.38, 0.72], [0.51, 0.56],
            [0.47, 0.42], [0.29, 0.44], [0.22, 0.62]
        ], this.panelDarkMaterial, { depth: 0.055, z: 0.145 });
        addMirrored(upperFaceRig, [
            [0.44, 0.5], [0.56, 0.4], [0.61, 0.2],
            [0.54, 0.12], [0.45, 0.25]
        ], this.panelDarkMaterial, { depth: 0.05, z: 0.125 });
        addMirrored(upperFaceRig, [
            [0.09, 0.36], [0.25, 0.42], [0.47, 0.34],
            [0.53, 0.23], [0.44, 0.11], [0.16, 0.15]
        ], this.panelMaterial, { depth: 0.055, z: 0.18 });
        addMirrored(upperFaceRig, [
            [0.15, 0.08], [0.43, 0.06], [0.55, -0.02],
            [0.52, -0.25], [0.35, -0.38], [0.13, -0.25]
        ], this.panelDarkMaterial, { depth: 0.06, z: 0.155 });
        addMirrored(upperFaceRig, [
            [0.5, 0.06], [0.6, 0.13], [0.6, -0.12],
            [0.52, -0.31], [0.44, -0.33], [0.49, -0.19]
        ], this.panelMaterial, { depth: 0.05, z: 0.12 });
        this.cheekRigs = addArticulatedPair(upperFaceRig, [
            [0.1, -0.14], [0.3, -0.16], [0.4, -0.27],
            [0.32, -0.41], [0.16, -0.43], [0.08, -0.33]
        ], [0.24, -0.29], this.panelMaterial, {
            depth: 0.055, z: 0.19
        });

        addMirrored(lowerJawPanels, [
            [0.16, -0.43], [0.34, -0.42], [0.46, -0.51],
            [0.38, -0.65], [0.23, -0.74], [0.11, -0.62]
        ], this.panelDarkMaterial, { depth: 0.06, z: 0.145 });
        addPlate(lowerJawPanels, [
            [-0.1, -0.52], [0.1, -0.52], [0.23, -0.65],
            [0.13, -0.8], [-0.13, -0.8], [-0.23, -0.65]
        ], this.panelMaterial, { depth: 0.065, z: 0.16 });

        this.upperMouthRigs = addArticulatedPair(upperFaceRig, [
            [0.018, -0.275], [0.12, -0.255], [0.25, -0.285],
            [0.205, -0.34], [0.075, -0.355], [0.018, -0.34]
        ], [0.125, -0.305], this.ridgeMaterial, {
            depth: 0.05, z: 0.215,
            bevelSize: 0.005, bevelThickness: 0.005
        });
        this.lowerMouthRigs = addArticulatedPair(lowerJawPanels, [
            [0.018, -0.35], [0.075, -0.365], [0.205, -0.355],
            [0.25, -0.4], [0.14, -0.46], [0.018, -0.435]
        ], [0.125, -0.4], this.panelDarkMaterial, {
            depth: 0.05, z: 0.205,
            bevelSize: 0.005, bevelThickness: 0.005
        });

        addPlate(upperFaceRig, [
            [-0.065, 0.39], [0.065, 0.39], [0.105, 0.14],
            [0.08, -0.09], [0, -0.16], [-0.08, -0.09],
            [-0.105, 0.14]
        ], this.ridgeMaterial, {
            depth: 0.075, z: 0.225,
            bevelSize: 0.006, bevelThickness: 0.006
        });

        this.leftEye = makeEye(
            -1, this.darkMaterial, this.eyeMaterial, this.eyeCoreMaterial
        );
        this.rightEye = makeEye(
            1, this.darkMaterial, this.eyeMaterial, this.eyeCoreMaterial
        );
        this.modelRoot.add(this.leftEye, this.rightEye);

        this.signalRig = new THREE.Group();
        this.signalRig.add(
            makeTube([[0, 0.72, 0.248], [0, 0.57, 0.252]],
                0.0018, this.signalMaterial),
            makeTube([[0, 0.31, 0.276], [0, -0.045, 0.279]],
                0.0015, this.signalMaterial),
            makeTube([[0.47, 0.59, 0.208], [0.52, 0.47, 0.211]],
                0.0023, this.signalMaterial),
            makeTube([[-0.47, 0.59, 0.208], [-0.52, 0.47, 0.211]],
                0.0023, this.signalMaterial),
            makeTube([[0.49, -0.06, 0.215], [0.46, -0.18, 0.218]],
                0.0018, this.signalMaterial),
            makeTube([[-0.49, -0.06, 0.215], [-0.46, -0.18, 0.218]],
                0.0018, this.signalMaterial)
        );
        this.modelRoot.add(this.signalRig);
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
            this.modelRoot.scale.setScalar(0.6);
            this.layoutX = 0;
            this.layoutY = 0.72;
        } else if (width < 1100) {
            this.modelRoot.scale.setScalar(0.94);
            this.layoutX = 0.42;
            this.layoutY = 0.05;
        } else if (this.workspaceOpen) {
            this.modelRoot.scale.setScalar(0.93);
            this.layoutX = 0;
            this.layoutY = 0.04;
        } else {
            this.modelRoot.scale.setScalar(1.06);
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
        this.edgeMaterial.color.lerp(targetColor, lerp * 0.52);
        this.signalMaterial.color.lerp(targetColor, lerp * 0.58);
        this.eyeMaterial.color.lerp(targetColor, lerp * 0.24);
        this.edgeMaterial.opacity += (
            style.opacity - this.edgeMaterial.opacity
        ) * lerp;
        var pulseSpeed = this.state === "thinking" ? 3.1 : 1.6;
        var signalPulse = this.reducedMotion
            ? 1
            : 0.88 + Math.sin(this.elapsed * pulseSpeed) * 0.12;
        this.signalMaterial.opacity += (
            style.signal * signalPulse - this.signalMaterial.opacity
        ) * lerp;
        var eyeOpacity = this.state === "listening" ? 1 : 0.82;
        this.eyeMaterial.opacity += (
            eyeOpacity - this.eyeMaterial.opacity
        ) * lerp;
        this.panelMaterials.forEach(function (material) {
            var tintStrength = this.state === "error" ? 0.11 : 0.035;
            var tinted = material.userData.baseColor.clone().lerp(
                targetColor, tintStrength
            );
            material.color.lerp(tinted, lerp * 0.16);
        }, this);

        var mouth = this.speechActive ? this.targetMouth : VISEME_SHAPES[0];
        this.currentMouthOpen += (
            mouth.open - this.currentMouthOpen
        ) * Math.min(1, delta * 22);
        this.currentMouthWidth += (
            mouth.width - this.currentMouthWidth
        ) * Math.min(1, delta * 18);
        var jawOpen = clamp(
            (this.currentMouthOpen - VISEME_SHAPES[0].open) / 0.88,
            0, 1
        );
        var widthShift = clamp(
            (this.currentMouthWidth - 1) * 0.055, -0.014, 0.014
        );
        var rounding = clamp(1 - this.currentMouthWidth, -0.25, 0.45);
        this.upperMouthRigs.forEach(function (rig) {
            var side = rig.userData.side;
            var closed = rig.userData.closedPosition;
            rig.position.x = closed.x + side * widthShift;
            rig.position.y = closed.y + jawOpen * 0.012;
            rig.position.z = jawOpen * 0.008;
            rig.rotation.x = -jawOpen * 0.055;
            rig.rotation.y = side * (rounding * 0.22 - jawOpen * 0.025);
            rig.rotation.z = -side * jawOpen * 0.015;
        });
        this.lowerMouthRigs.forEach(function (rig) {
            var side = rig.userData.side;
            var closed = rig.userData.closedPosition;
            rig.position.x = closed.x + side * widthShift * 0.86;
            rig.position.y = closed.y - jawOpen * 0.012;
            rig.position.z = jawOpen * 0.004;
            rig.rotation.x = jawOpen * 0.09;
            rig.rotation.y = side * (rounding * 0.18 + jawOpen * 0.02);
            rig.rotation.z = side * jawOpen * 0.012;
        });
        this.cheekRigs.forEach(function (rig) {
            var side = rig.userData.side;
            var closed = rig.userData.closedPosition;
            rig.position.x = closed.x + side * (
                jawOpen * 0.008 + Math.max(widthShift, 0) * 0.2
            );
            rig.rotation.y = -side * jawOpen * 0.025;
            rig.rotation.z = -side * jawOpen * 0.008;
        });
        var jawClosed = this.lowerJawRig.userData.closedPosition;
        this.lowerJawRig.position.y = jawClosed.y - jawOpen * 0.045;
        this.lowerJawRig.position.z = jawClosed.z - jawOpen * 0.012;
        this.lowerJawRig.rotation.x = jawOpen * 0.13;
        this.canvas.dataset.mouthOpen = this.currentMouthOpen.toFixed(3);
        this.canvas.dataset.viseme = String(this.currentViseme);

        var blinkScale = this._blinkScale();
        var eyeStateScale = this.state === "listening" ? 1.14 : 1;
        this.leftEye.userData.aperture.scale.y = blinkScale * eyeStateScale;
        this.rightEye.userData.aperture.scale.y = blinkScale * eyeStateScale;
        this.smoothedAudio += (
            this.audioLevel - this.smoothedAudio
        ) * Math.min(1, delta * 12);
        var pupilScale = 1 + this.smoothedAudio * 0.3;
        [this.leftEye, this.rightEye].forEach(function (eye) {
            var side = eye.userData.side;
            var gazeX = this.pointer.x * 0.012;
            var gazeY = this.pointer.y * 0.01;
            if (this.state === "thinking") {
                gazeX += side * 0.003;
                gazeY += 0.018;
            }
            eye.userData.gaze.position.x += (
                gazeX * side - eye.userData.gaze.position.x
            ) * lerp;
            eye.userData.gaze.position.y += (
                gazeY - eye.userData.gaze.position.y
            ) * lerp;
            eye.userData.gaze.scale.setScalar(pupilScale);
        }, this);

        var drift = this.reducedMotion
            ? 0
            : Math.sin(this.elapsed * 0.48) * 0.012;
        this.faceRoot.rotation.y += (
            this.baseYaw + this.pointer.x * 0.05 + drift
            - this.faceRoot.rotation.y
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
            design: "segmented-mask",
            mouthMechanism: "articulated-plates",
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
