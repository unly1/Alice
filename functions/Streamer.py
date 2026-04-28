import re
import queue
import threading
import time


class AudioStreamer:
    """Quebra o texto da IA em frases e gerencia a fila de áudio."""

    def __init__(self):
        self.fila_audio = queue.Queue()
        self.thread_fala = threading.Thread(target=self._processar_fila, daemon=True)
        self.thread_fala.start()

    def processar_texto_stream(self, buffer_texto):
        """
        Recebe texto vindos da IA (em fluxo).
        Quebra em sentenças usando pontuação e envia para a fila.
        """
        # Em uma implementação real, analisaríamos letras uma a uma até encontrar: (., !, ?)
        # E só então o trecho seria jogado na fila de áudio e limpo do buffer.

        frases = re.split(r"(?<=[.!?]) +", buffer_texto)
        for frase in frases:
            if frase.strip():
                print(f"[Streamer] Sentença detectada e enviada para TTS: {frase}")
                self.fila_audio.put(frase)

    def _processar_fila(self):
        """Thread paralela que consome a fila e lê em voz alta enquanto a IA 'pensa' no resto das respostas."""
        while True:
            frase = self.fila_audio.get()
            if frase is None:
                break
            print(f"[🗣️ Processando fala sem delay (Low Latency)]: {frase}")

            # Aqui entraríamos com Edge-TTS ou mecanismo de voz. A gravação seria tocada instantaneamente.
            time.sleep(len(frase) * 0.05)  # Simulação de tempo para leitura humana

            self.fila_audio.task_done()
