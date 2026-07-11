# 语音口型与人脸方案

更新日期：2026-07-11

## 结论

本项目采用 **Azure Speech SDK + 项目内极简低多边形网格脸**：

```text
LLM 文本
  -> Flask /api/speech
  -> Azure TTS 音频 + viseme ID + audio offset
  -> Web Audio 播放时钟
  -> Three.js 网格脸嘴部参数
```

这条链路的关键是音频和口型事件来自同一次合成。浏览器打字速度、字符类别和
`setTimeout` 都不能作为口型时间源。

## 成熟方案对比

| 方案 | 能力 | 中文与实时性 | 本项目判断 |
|---|---|---|---|
| Azure Speech SDK | 22 个 viseme ID、音频 offset；可输出 55 个 60 FPS blend shapes | `zh-CN` 支持 viseme ID 和 blend shapes | 当前最合适，接口稳定且同步信息完整 |
| TalkingHead | Three.js 实时 avatar，支持 ARKit/Oculus morph targets 和多种 TTS 时间轴 | 取决于 TTS；avatar 必须自带标准 morph targets | 适合未来换完整数字人，当前引入成本高于极简脸 |
| NVIDIA Audio2Face | 从音频生成高质量完整面部动画 | 实时效果强 | 依赖 NVIDIA GPU、容器和 gRPC，不适合当前 Mac 本地 Web 应用 |
| AWS Polly | 音频与 viseme speech marks | 有时间戳 | 可用，但 3D blendshape 集成不如 Azure 直接 |
| Rhubarb Lip Sync | WAV/OGG 离线生成 6-9 种口型 | 非英语 phonetic 模式精度有限 | 适合离线卡通动画，不适合实时中文回复 |
| HeadAudio | 浏览器内从实际音频识别 viseme | 可实时 | 项目较新，可作为离线方向实验，不作为当前主链路 |

TalkingHead 本身只解决 avatar 渲染和动画消费，不会凭空产生准确中文音素时间轴。
如果以后使用 Avaturn 或 Ready Player Me 一类带标准 morph targets 的 GLB，可以保留
当前 `/api/speech` 契约，仅替换 `JarvisFace` 渲染器。

## 为什么停用浏览器 TTS 伪口型

Web Speech API 的 `boundary` 事件公开的是词或句边界及字符位置，不提供真实音素、
viseme，也不提供可供分析的合成音频。按汉字、正则或字符编码选择嘴型，会出现三个
不可修复的问题：

1. 中文字符与实际发音不是一一对应。
2. 文本进度与语音播放进度没有共同时间基准。
3. 浏览器、系统语音和语速变化会让误差不断累积。

因此未配置精确 TTS 时，系统会明确提示不可用，不会再制造“嘴在动所以已经同步”的
假象。

## 参考资料

- [Azure Speech viseme 官方文档](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/how-to-speech-synthesis-viseme)
- [Azure Speech 语言与 viseme 支持](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/language-support)
- [TalkingHead](https://github.com/met4citizen/TalkingHead)
- [NVIDIA Audio2Face-3D](https://docs.nvidia.com/ace/audio2face-3d-microservice/latest/)
- [AWS Polly viseme](https://docs.aws.amazon.com/polly/latest/dg/viseme.html)
- [Rhubarb Lip Sync](https://github.com/DanielSWolf/rhubarb-lip-sync)
- [HeadAudio](https://github.com/met4citizen/HeadAudio)
- [MDN SpeechSynthesisUtterance boundary event](https://developer.mozilla.org/en-US/docs/Web/API/SpeechSynthesisUtterance/boundary_event)
