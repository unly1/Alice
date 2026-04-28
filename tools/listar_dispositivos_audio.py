"""
Lista todos os dispositivos de áudio disponíveis no sistema.
Execute este script para identificar os índices e nomes dos dispositivos
antes de configurar o .env ou a aba "🎙️ Áudio" da Alice.

Uso:
    python tools/listar_dispositivos_audio.py
"""

import pyaudiowpatch as pyaudio


def listar_dispositivos():
    pa = pyaudio.PyAudio()

    print("\n=== DISPOSITIVOS DE ÁUDIO ===\n")
    print(f"{'Idx':>4}  {'Nome':<52}  {'In':>3}  {'Out':>3}  {'Rate':>7}")
    print("-" * 78)

    entradas = []
    saidas = []

    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        nome = d["name"]
        ch_in = int(d["maxInputChannels"])
        ch_out = int(d["maxOutputChannels"])
        rate = int(d["defaultSampleRate"])
        print(f"{i:>4}  {nome:<52}  {ch_in:>3}  {ch_out:>3}  {rate:>7}")

        if ch_in > 0:
            entradas.append((i, nome))
        if ch_out > 0:
            saidas.append((i, nome))

    print("\n=== ENTRADAS (microfones) ===")
    for idx, nome in entradas:
        print(f"  [{idx}] {nome}")

    print("\n=== SAÍDAS (alto-falantes / virtuais) ===")
    for idx, nome in saidas:
        print(f"  [{idx}] {nome}")

    print("\n=== COMO USAR ===")
    print(
        "  Use a aba '🎙️ Áudio' na interface da Alice para selecionar os dispositivos,"
    )
    print("  ou edite o .env manualmente com os valores acima:")
    print("    INDICE_MICROFONE=<índice da entrada>")
    print("    DISPOSITIVO_SAIDA_AUDIO=<nome da saída>")

    pa.terminate()


if __name__ == "__main__":
    listar_dispositivos()
