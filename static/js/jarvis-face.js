(function () {
    "use strict";

    var STATE_STYLE = {
        idle: { color: 0x56d6d2, eye: 1, brow: 0, gazeX: 0, gazeY: 0, tilt: 0, scan: 0.24, glow: 0.78 },
        listening: { color: 0x91e6b5, eye: 1.18, brow: 0.025, gazeX: 0, gazeY: 0, tilt: -0.035, scan: 0.42, glow: 1 },
        thinking: { color: 0xffc857, eye: 0.92, brow: 0.05, gazeX: 0.035, gazeY: 0.035, tilt: 0.025, scan: 0.9, glow: 0.9 },
        speaking: { color: 0x56d6d2, eye: 1, brow: 0.008, gazeX: 0, gazeY: -0.008, tilt: 0, scan: 0.48, glow: 1 },
        executing: { color: 0x91e6b5, eye: 0.88, brow: -0.025, gazeX: 0, gazeY: 0.02, tilt: 0, scan: 1.25, glow: 1 },
        error: { color: 0xff6b6b, eye: 0.72, brow: -0.075, gazeX: 0, gazeY: -0.02, tilt: 0, scan: 1.8, glow: 1 }
    };

    function curveGeometry(points, closed, radius) {
        var vectors = points.map(function (point) {
            return new THREE.Vector3(point[0], point[1], point[2] || 0.52);
        });
        var curve = new THREE.CatmullRomCurve3(vectors, Boolean(closed), "catmullrom", 0.42);
        return new THREE.TubeGeometry(curve, Math.max(24, vectors.length * 10), radius || 0.006, 5, Boolean(closed));
    }

    function curveMesh(points, material, closed, radius) {
        return new THREE.Mesh(curveGeometry(points, closed, radius), material);
    }

    function material(color, opacity) {
        return new THREE.MeshBasicMaterial({
            color: color,
            transparent: true,
            opacity: opacity,
            blending: THREE.AdditiveBlending,
            depthWrite: false
        });
    }

    function JarvisFace(canvas, options) {
        options = options || {};
        if (!window.THREE) {
            throw new Error("Three.js is unavailable");
        }
        this.canvas = canvas;
        this.reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        this.state = "idle";
        this.audioLevel = 0;
        this.smoothedAudio = 0;
        this.pointer = { x: 0, y: 0 };
        this.clock = new THREE.Clock();
        this.elapsed = 0;
        this.frameAccumulator = 0;
        this.nextBlink = 2.5 + Math.random() * 2.5;
        this.blinkStarted = -1;
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
        this.camera = new THREE.PerspectiveCamera(34, 1, 0.1, 100);
        this.camera.position.set(0, 0, 5.2);
        this.faceRoot = new THREE.Group();
        this.scene.add(this.faceRoot);

        this.featureMaterial = material(0x56d6d2, 0.86);
        this.accentMaterial = material(0x91e6b5, 0.92);
        this.softMaterial = material(0x56d6d2, 0.18);
        this.scanMaterial = material(0x56d6d2, 0.1);
        this.materials = [
            this.featureMaterial,
            this.accentMaterial,
            this.softMaterial,
            this.scanMaterial
        ];

        this._buildHead();
        this._buildEyes();
        this._buildNoseAndMouth();
        this._buildScanField();
        this.setState("idle");
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

    JarvisFace.prototype._buildHead = function () {
        var shellGeometry = new THREE.SphereGeometry(1, 28, 34);
        shellGeometry.scale(0.79, 1.08, 0.58);
        var wire = new THREE.WireframeGeometry(shellGeometry);
        this.shell = new THREE.LineSegments(wire, new THREE.LineBasicMaterial({
            color: 0x56d6d2,
            transparent: true,
            opacity: 0.055,
            blending: THREE.AdditiveBlending,
            depthWrite: false
        }));
        this.faceRoot.add(this.shell);

        var contour = [
            [-0.56, 0.78], [-0.72, 0.48], [-0.76, 0.08], [-0.68, -0.45],
            [-0.43, -0.86], [0, -1.03], [0.43, -0.86], [0.68, -0.45],
            [0.76, 0.08], [0.72, 0.48], [0.56, 0.78], [0, 0.98]
        ];
        this.faceRoot.add(curveMesh(contour, this.featureMaterial, true, 0.008));

        var leftCheek = curveMesh([
            [-0.63, 0.02], [-0.56, -0.23], [-0.43, -0.49], [-0.22, -0.62]
        ], this.softMaterial, false, 0.005);
        var rightCheek = leftCheek.clone();
        rightCheek.scale.x = -1;
        this.faceRoot.add(leftCheek, rightCheek);

        var leftTemple = curveMesh([
            [-0.68, 0.42], [-0.77, 0.25], [-0.78, -0.06]
        ], this.accentMaterial, false, 0.009);
        var rightTemple = leftTemple.clone();
        rightTemple.scale.x = -1;
        this.faceRoot.add(leftTemple, rightTemple);

        this.templeNodes = [];
        [-1, 1].forEach(function (side) {
            var node = new THREE.Mesh(
                new THREE.CircleGeometry(0.022, 16),
                material(0x91e6b5, 0.9)
            );
            node.position.set(side * 0.75, 0.18, 0.545);
            this.faceRoot.add(node);
            this.templeNodes.push(node);
        }, this);
    };

    JarvisFace.prototype._buildEyes = function () {
        this.eyeGroups = [];
        this.irises = [];
        this.brows = [];
        var self = this;
        [-1, 1].forEach(function (side) {
            var eye = new THREE.Group();
            eye.position.x = side * 0.31;
            var top = curveMesh([
                [-0.19, 0], [-0.1, 0.065], [0, 0.075], [0.1, 0.052], [0.19, 0]
            ], self.featureMaterial, false, 0.008);
            var bottom = curveMesh([
                [-0.19, 0], [-0.1, -0.045], [0, -0.058], [0.1, -0.04], [0.19, 0]
            ], self.featureMaterial, false, 0.006);
            eye.add(top, bottom);

            var iris = new THREE.Group();
            var irisRing = new THREE.Mesh(
                new THREE.RingGeometry(0.052, 0.067, 32),
                self.accentMaterial
            );
            var pupil = new THREE.Mesh(
                new THREE.CircleGeometry(0.018, 20),
                material(0xedf6f5, 0.96)
            );
            irisRing.position.z = 0.006;
            pupil.position.z = 0.009;
            iris.add(irisRing, pupil);
            eye.add(iris);
            self.faceRoot.add(eye);
            self.eyeGroups.push(eye);
            self.irises.push(iris);

            var browPoints = side < 0
                ? [[-0.49, 0.39], [-0.39, 0.44], [-0.25, 0.43], [-0.13, 0.38]]
                : [[0.13, 0.38], [0.25, 0.43], [0.39, 0.44], [0.49, 0.39]];
            var brow = curveMesh(browPoints, self.featureMaterial, false, 0.009);
            self.faceRoot.add(brow);
            self.brows.push(brow);
        });
        this.eyeGroups.forEach(function (eye) { eye.position.y = 0.22; });
    };

    JarvisFace.prototype._buildNoseAndMouth = function () {
        this.nose = curveMesh([
            [0, 0.34], [-0.018, 0.14], [-0.04, -0.08], [-0.11, -0.25],
            [0, -0.29], [0.11, -0.25]
        ], this.softMaterial, false, 0.006);
        this.faceRoot.add(this.nose);

        this.mouth = new THREE.Group();
        this.mouth.position.y = -0.51;
        this.mouthTop = curveMesh([
            [-0.25, 0], [-0.13, 0.035], [0, 0.018], [0.13, 0.035], [0.25, 0]
        ], this.featureMaterial, false, 0.008);
        this.mouthBottom = curveMesh([
            [-0.25, 0], [-0.13, -0.035], [0, -0.046], [0.13, -0.035], [0.25, 0]
        ], this.featureMaterial, false, 0.007);
        this.mouth.add(this.mouthTop, this.mouthBottom);
        this.faceRoot.add(this.mouth);

        this.jaw = curveMesh([
            [-0.43, -0.67], [-0.24, -0.84], [0, -0.91], [0.24, -0.84], [0.43, -0.67]
        ], this.softMaterial, false, 0.006);
        this.faceRoot.add(this.jaw);

        var forehead = curveMesh([
            [-0.34, 0.69], [0, 0.78], [0.34, 0.69]
        ], this.softMaterial, false, 0.005);
        this.faceRoot.add(forehead);
    };

    JarvisFace.prototype._buildScanField = function () {
        this.scanGroup = new THREE.Group();
        for (var index = 0; index < 17; index += 1) {
            var y = -0.86 + index * 0.105;
            var normalized = Math.abs(y / 1.04);
            var halfWidth = Math.max(0.12, 0.72 * Math.sqrt(Math.max(0, 1 - normalized * normalized)));
            var geometry = new THREE.BufferGeometry().setFromPoints([
                new THREE.Vector3(-halfWidth, y, 0.48),
                new THREE.Vector3(halfWidth, y, 0.48)
            ]);
            var line = new THREE.Line(geometry, new THREE.LineBasicMaterial({
                color: 0x56d6d2,
                transparent: true,
                opacity: index % 3 === 0 ? 0.09 : 0.025,
                blending: THREE.AdditiveBlending,
                depthWrite: false
            }));
            this.scanGroup.add(line);
        }
        this.faceRoot.add(this.scanGroup);

        this.scanBand = new THREE.Mesh(
            new THREE.PlaneGeometry(1.45, 0.018),
            this.scanMaterial
        );
        this.scanBand.position.z = 0.57;
        this.faceRoot.add(this.scanBand);
    };

    JarvisFace.prototype.setState = function (state) {
        if (!STATE_STYLE[state]) {
            return;
        }
        this.state = state;
    };

    JarvisFace.prototype.setAudioLevel = function (level) {
        this.audioLevel = Math.max(0, Math.min(1, Number(level) || 0));
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
            this.faceRoot.scale.setScalar(0.72);
            this.layoutX = 0;
            this.layoutY = 0.62;
        } else if (width < 1100) {
            this.faceRoot.scale.setScalar(0.88);
            this.layoutX = 0.45;
            this.layoutY = 0.02;
        } else {
            this.faceRoot.scale.setScalar(this.workspaceOpen ? 0.86 : 1);
            this.layoutX = this.workspaceOpen ? 0 : 0.42;
            this.layoutY = 0.01;
        }
    };

    JarvisFace.prototype._blinkAmount = function () {
        if (this.reducedMotion) {
            return 1;
        }
        if (this.elapsed >= this.nextBlink && this.blinkStarted < 0) {
            this.blinkStarted = this.elapsed;
        }
        if (this.blinkStarted < 0) {
            return 1;
        }
        var progress = (this.elapsed - this.blinkStarted) / 0.16;
        if (progress >= 1) {
            this.blinkStarted = -1;
            this.nextBlink = this.elapsed + 2.8 + Math.random() * 3.8;
            return 1;
        }
        return 0.12 + Math.abs(progress - 0.5) * 1.76;
    };

    JarvisFace.prototype._render = function () {
        if (document.hidden) {
            this.clock.getDelta();
            return;
        }
        var delta = Math.min(this.clock.getDelta(), 0.05);
        this.frameAccumulator += delta;
        var frameInterval = this.reducedMotion
            ? 0.125
            : (this.state === "idle" ? 1 / 30 : 1 / 60);
        if (this.frameAccumulator < frameInterval) {
            return;
        }
        delta = Math.min(this.frameAccumulator, 0.125);
        this.frameAccumulator = 0;
        this.elapsed += delta;
        var style = STATE_STYLE[this.state];
        var lerp = 1 - Math.pow(0.001, delta);
        var targetColor = new THREE.Color(style.color);

        this.materials.forEach(function (item) {
            item.color.lerp(targetColor, lerp * 0.72);
        });
        this.shell.material.color.lerp(targetColor, lerp * 0.72);
        this.scanGroup.children.forEach(function (line) {
            line.material.color.lerp(targetColor, lerp * 0.72);
        });

        var blink = this._blinkAmount();
        this.eyeGroups.forEach(function (eye) {
            eye.scale.y += (style.eye * blink - eye.scale.y) * lerp;
        });

        this.smoothedAudio += (this.audioLevel - this.smoothedAudio) * Math.min(1, delta * 12);
        var gazeX = style.gazeX + this.pointer.x * 0.018;
        var gazeY = style.gazeY + this.pointer.y * 0.012;
        this.irises.forEach(function (iris) {
            iris.position.x += (gazeX - iris.position.x) * lerp;
            iris.position.y += (gazeY - iris.position.y) * lerp;
            var irisScale = this.state === "listening"
                ? 1.08 + this.smoothedAudio * 0.22
                : 1;
            iris.scale.setScalar(iris.scale.x + (irisScale - iris.scale.x) * lerp);
        }, this);

        this.brows[0].rotation.z += ((-style.brow) - this.brows[0].rotation.z) * lerp;
        this.brows[1].rotation.z += (style.brow - this.brows[1].rotation.z) * lerp;

        var mouthLevel = 0;
        if (this.state === "speaking" && !this.reducedMotion) {
            mouthLevel = Math.max(this.smoothedAudio, 0.2 + Math.abs(Math.sin(this.elapsed * 9.5)) * 0.34);
        } else if (this.state === "listening") {
            mouthLevel = this.smoothedAudio * 0.08;
        }
        var frown = this.state === "error" ? -0.035 : 0;
        this.mouthTop.position.y += ((mouthLevel * 0.07 + frown) - this.mouthTop.position.y) * lerp;
        this.mouthBottom.position.y += ((-mouthLevel * 0.12 + frown) - this.mouthBottom.position.y) * lerp;
        this.jaw.position.y += ((-mouthLevel * 0.055) - this.jaw.position.y) * lerp;

        var drift = this.reducedMotion ? 0 : Math.sin(this.elapsed * 0.55) * 0.012;
        this.faceRoot.rotation.y += ((this.pointer.x * 0.035 + drift) - this.faceRoot.rotation.y) * lerp;
        this.faceRoot.rotation.x += ((-this.pointer.y * 0.02) - this.faceRoot.rotation.x) * lerp;
        this.faceRoot.rotation.z += (style.tilt - this.faceRoot.rotation.z) * lerp;
        this.faceRoot.position.x += (this.layoutX - this.faceRoot.position.x) * lerp;
        this.faceRoot.position.y += (this.layoutY - this.faceRoot.position.y) * lerp;

        var scanY = this.reducedMotion
            ? 0
            : ((this.elapsed * style.scan) % 2.05) - 1.02;
        this.scanBand.position.y = scanY;
        this.scanBand.material.opacity = 0.1 * style.glow;
        this.featureMaterial.opacity += (0.86 * style.glow - this.featureMaterial.opacity) * lerp;

        this.templeNodes.forEach(function (node, index) {
            var pulse = this.reducedMotion
                ? 1 + (this.state === "listening" ? this.smoothedAudio * 0.12 : 0)
                : 0.7 + Math.sin(this.elapsed * 3.2 + index * Math.PI) * 0.25
                    + (this.state === "listening" ? this.smoothedAudio * 0.18 : 0);
            node.scale.setScalar(pulse);
        }, this);

        if (this.state === "error" && !this.reducedMotion) {
            this.faceRoot.position.x += (Math.random() - 0.5) * 0.004;
        }
        this.renderer.render(this.scene, this.camera);
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
