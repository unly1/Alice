import sys, os
sys.path.insert(0, r'c:\Users\moliv\Documents\Projeto Alice')
os.chdir(r'c:\Users\moliv\Documents\Projeto Alice')

print('=== Teste Completo Projeto Alice v2 ===')

# Config
from core.config.loader import load_config
cfg = load_config('conf.yaml')
print(f'[1] Config OK: {cfg.agent.modelo_ia} / {cfg.vad.vad_model} / {cfg.tts.tts_model}')

# VAD RMS (sem GPU)
from core.vad.vad_factory import get_vad
from core.config.models import VADConfig
import numpy as np
rms_cfg = VADConfig(vad_model='rms')
vad = get_vad(rms_cfg)
from core.vad.vad_interface import VADResult
r = vad.process_chunk(np.zeros(512, dtype=np.float32))
print(f'[2] RmsVAD OK: resultado={r}')

# Silero import
from core.vad.silero_vad import SileroVAD
print('[3] SileroVAD import OK')

# ASR Factory
from core.asr.asr_factory import get_asr
print('[4] ASR Factory import OK')

# TTS Factory
from core.tts.tts_factory import get_tts
print('[5] TTS Factory import OK')

# Sentence divider
import asyncio
from core.tts.sentence_divider import sentence_divider

async def test_divider():
    async def fake_stream():
        tokens = 'Ola! Como vai voce? Estou bem, obrigado. Posso ajudar?'
        for ch in tokens:
            yield ch
    sentences = []
    async for s in sentence_divider(fake_stream()):
        sentences.append(s)
    return sentences

sentences = asyncio.run(test_divider())
print(f'[6] SentenceDivider OK: {len(sentences)} sentencas detectadas')
for s in sentences:
    print(f'    -> "{s}"')

# Agent
from core.agent.agent_interface import AgentInterface
from core.agent.agent_factory import get_agent
print('[7] Agent Factory import OK')

# ServiceContext
from core.service_context import ServiceContext
print('[8] ServiceContext import OK')

print()
print('=== TODOS OS TESTES PASSARAM ===')
