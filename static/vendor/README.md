# Vendored Frontend Dependencies

These files are committed so the interface does not depend on a CDN at runtime.

| Library | Version | File | SHA-256 |
|---|---:|---|---|
| Three.js | r128 (`0.128.0`) | `three/three.min.js` | `9274bbcec8d96168626c732b5d31c775aa8cfb7eaa0599bec0c175908a2c1ce2` |
| Lucide | `0.468.0` | `lucide/lucide.min.js` | `3411692820cb8d47543f69496aa25fd603a358f4498046f41c508a5a3342210e` |

The matching license text is stored beside each library. Verify the checksum after
any replacement. Three.js r128 is retained because this UI uses its legacy global
build; upgrading to the current module build requires a visual regression pass for
color output, geometry disposal, desktop/mobile framing, and all face states.
