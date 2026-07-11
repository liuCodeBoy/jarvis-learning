# Vendored Frontend Dependencies

These files are committed so the interface does not depend on a CDN at runtime.

| Library | Version | File | SHA-256 |
|---|---:|---|---|
| Three.js | r128 (`0.128.0`) | `three/three.min.js` | `9274bbcec8d96168626c732b5d31c775aa8cfb7eaa0599bec0c175908a2c1ce2` |
| Three.js GLTFLoader | r128 (`0.128.0`) | `three/examples/js/loaders/GLTFLoader.js` | `5c15967ba830918a9caea6338712c994c354bccd4edc4569bde411c3ec06a3e6` |
| Lucide | `0.468.0` | `lucide/lucide.min.js` | `3411692820cb8d47543f69496aa25fd603a358f4498046f41c508a5a3342210e` |

The matching license text is stored beside each library. Verify the checksum after
any replacement. Three.js r128 is retained because this UI uses its legacy global
build; upgrading to the current module build requires a visual regression pass for
color output, geometry disposal, desktop/mobile framing, and all face states.

## Face model

`static/models/lee-perry-smith/LeePerrySmith.glb` is the Lee Perry-Smith 3D
head scan distributed by the Three.js project. It is licensed under CC BY 3.0;
the attribution and license notice are stored beside the model. Local copy
SHA-256: `402b8a8ac9f03232e6d64b5962929703a069daf99d3c49ac8eb0e48bedc9c576`.
