"""J.A.R.V.I.S. 自学习自进化系统

基于Hermes架构、融合多维度学习机制与Darwinian进化引擎的智能自适应平台。
"""

__version__ = "4.1.0"

__all__ = ['LLMConfig', 'get_llm']


def __getattr__(name):
    if name in __all__:
        from jarvis.core.llm import LLMConfig, get_llm
        return {"LLMConfig": LLMConfig, "get_llm": get_llm}[name]
    raise AttributeError(name)
