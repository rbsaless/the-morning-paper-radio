#!/usr/bin/env python3
"""Rádio em lote: coleta notícias e clima, escreve roteiros (Gemini), gera voz
(Gemini TTS, com reserva Edge TTS) e publica um programa em docs/programas/."""
import asyncio, hashlib, html, json, os, re, shutil, subprocess, sys, tempfile, time
import urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

RAIZ = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(RAIZ, "docs")
ESTADO = os.path.join(RAIZ, "estado.json")
CFG = json.load(open(os.path.join(RAIZ, "config.json"), encoding="utf-8"))
CHAVE = os.environ.get("GEMINI_API_KEY", "").strip()
MODELOS_TEXTO = [m for m in [os.environ.get("GEMINI_MODEL"), "gemini-flash-lite-latest",
                             "gemini-3.5-flash-lite", "gemini-3.8-flash"] if m]
MODELOS_VOZ = [m for m in [os.environ.get("GEMINI_TTS_MODEL"), "gemini-2.5-flash-preview-tts",
                           "gemini-2.5-pro-preview-tts"] if m]
TAXA = 24000
PAUSA_TTS = 6          # segundos entre chamadas do Gemini TTS (respeita o limite por minuto)
MAX_CHARS_TRECHO = 2200
gemini_voz_ligado = True
falhas_gemini_voz = 0


# ---------- utilidades ----------
def limpar(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def http(url, t=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (radio-bot)"})
    with urllib.request.urlopen(req, timeout=t) as r:
        return r.read()


def silencio(seg):
    return b"\x00" * (int(TAXA * seg) * 2)


def hash_titulo(t):
    return hashlib.md5(re.sub(r"\W+", " ", t.lower()).strip()[:120].encode()).hexdigest()[:16]


def data_item(txt):
    if not txt:
        return None
    try:
        d = parsedate_to_datetime(txt)
    except Exception:
        try:
            d = datetime.fromisoformat(txt.strip().replace("Z", "+00:00"))
        except Exception:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ---------- notícias ----------
def ler_feed(f):
    raiz = ET.fromstring(http(f["url"]))
    itens = []
    for e in raiz.iter():
        if e.tag.split("}")[-1] not in ("item", "entry"):
            continue

        def pega(*nomes):
            for c in e:
                if c.tag.split("}")[-1] in nomes and (c.text or "").strip():
                    return c.text
            return ""

        titulo = limpar(pega("title"))
        resumo = limpar(pega("description", "summary", "content"))[:350]
        if resumo[:30] == titulo[:30]:
            resumo = ""
        if titulo:
            itens.append({"titulo": titulo, "resumo": resumo, "fonte": f["nome"], "cat": f["cat"],
                          "data": data_item(pega("pubDate", "published", "updated", "date")),
                          "h": hash_titulo(titulo)})
    return itens


def coletar():
    corte = datetime.now(timezone.utc) - timedelta(hours=CFG["horas_recentes"])
    pool, vistos_h = [], set()
    for f in CFG["fontes"]:
        try:
            itens = [i for i in ler_feed(f) if i["data"] is None or i["data"] >= corte]
        except Exception as e:
            print(f"[aviso] feed {f['nome']}: {e}")
            continue
        itens.sort(key=lambda i: i["data"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        n = 0
        for i in itens:
            if i["h"] in vistos_h:
                continue
            vistos_h.add(i["h"])
            pool.append(i)
            n += 1
            if n >= f["max"]:
                break
        print(f"Feed {f['nome']}: {n} itens")
    return pool


def escolher(pool, cats, n, vistos):
    por_fonte = {}
    for i in pool:
        if i["h"] in vistos or (cats and i["cat"] not in cats):
            continue
        por_fonte.setdefault(i["fonte"], []).append(i)
    saida = []
    while len(saida) < n and any(por_fonte.values()):
        for k in list(por_fonte):
            if por_fonte[k] and len(saida) < n:
                saida.append(por_fonte[k].pop(0))
    return saida


def linha_item(i):
    return f"- [{i['fonte']}] {i['titulo']}" + (f". {i['resumo']}" if i["resumo"] else "")


# ---------- clima (Open-Meteo, sem chave) ----------
COD = {0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain",
       65: "heavy rain", 71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
       81: "rain showers", 82: "violent rain showers", 95: "thunderstorm",
       96: "thunderstorm with hail", 99: "thunderstorm with hail"}


def clima(c):
    q = urllib.parse.urlencode({
        "latitude": c["lat"], "longitude": c["lon"], "timezone": "auto", "forecast_days": 1,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max"})
    d = json.loads(http("https://api.open-meteo.com/v1/forecast?" + q))
    cur, dia = d["current"], d["daily"]
    return (f"- {c['nome']}: now {cur['temperature_2m']} C (feels like {cur['apparent_temperature']} C), "
            f"{COD.get(cur['weather_code'], 'mixed conditions')}, wind {cur['wind_speed_10m']} km/h; "
            f"today min {dia['temperature_2m_min'][0]} C, max {dia['temperature_2m_max'][0]} C, "
            f"chance of rain {dia['precipitation_probability_max'][0]}%")


# ---------- Gemini ----------
def gemini(modelo, corpo, t=180):
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent",
        data=json.dumps(corpo).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": CHAVE})
    with urllib.request.urlopen(req, timeout=t) as r:
        return json.load(r)["candidates"][0]["content"]["parts"][0]


def roteiro(b, material, primeiro, ultimo):
    nomes = list(b["vozes"])
    p = (f"{CFG['estilo']}\n\nBLOCK: {b['titulo']}. {b['prompt']}\n"
         f"Target length: about {b['palavras']} words.\n"
         + (f"Speaker: {nomes[0]} only.\n" if len(nomes) == 1
            else f"Speakers: {' and '.join(nomes)}. Both must speak, alternating naturally.\n")
         + (f"The first line must be exactly: {b['fala_inicial']}\n" if b.get("fala_inicial") and primeiro else "")
         + ("This is the final block: end with a short sign-off for the whole program.\n" if ultimo
            else "Do not greet the audience and do not say goodbye; the show continues after this block.\n")
         + "Format STRICTLY as lines starting with '<Name>:' and nothing else.\n\nMATERIAL:\n"
         + "\n".join(material))
    padrao = re.compile(r"^(" + "|".join(map(re.escape, nomes)) + r"):")
    for m in MODELOS_TEXTO:
        try:
            t = gemini(m, {"contents": [{"parts": [{"text": p}]}],
                           "generationConfig": {"temperature": 0.7}})["text"]
            linhas = [l.strip().replace("*", "") for l in t.splitlines()]
            linhas = [l for l in linhas if padrao.match(l)]
            if len(linhas) >= 4:
                print(f"  Roteiro {m}: {len(linhas)} falas")
                return linhas
        except Exception as e:
            print(f"  [aviso] roteiro {m}: {e}")
        time.sleep(3)
    return None


def partir(linhas):
    trecho, tam = [], 0
    for l in linhas:
        if trecho and tam + len(l) > MAX_CHARS_TRECHO:
            yield trecho
            trecho, tam = [], 0
        trecho.append(l)
        tam += len(l)
    if trecho:
        yield trecho


# ---------- voz ----------
def tts_gemini(trecho, vozes):
    global gemini_voz_ligado, falhas_gemini_voz
    if not gemini_voz_ligado:
        return None
    nomes = list(vozes)
    if len(nomes) == 1:
        cfg = {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": vozes[nomes[0]]["gemini"]}}}
        texto = "Read this as a warm, clear radio presenter:\n" + "\n".join(l.split(":", 1)[1].strip() for l in trecho)
    else:
        cfg = {"multiSpeakerVoiceConfig": {"speakerVoiceConfigs": [
            {"speaker": k, "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": v["gemini"]}}}
            for k, v in vozes.items()]}}
        texto = "Read this as a lively, friendly radio conversation:\n" + "\n".join(trecho)
    corpo = {"contents": [{"parts": [{"text": texto}]}],
             "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": cfg}}
    import base64
    for m in MODELOS_VOZ:
        for tentativa in range(2):
            try:
                pcm = base64.b64decode(gemini(m, corpo, 300)["inlineData"]["data"])
                falhas_gemini_voz = 0
                time.sleep(PAUSA_TTS)
                return pcm
            except Exception as e:
                print(f"  [aviso] voz {m}: {e}")
                if "429" in str(e) and tentativa == 0:
                    time.sleep(30)
                    continue
                break
    falhas_gemini_voz += 1
    if falhas_gemini_voz >= 2:
        gemini_voz_ligado = False
        print("  [aviso] Gemini TTS desligado nesta execução; usando Edge TTS.")
    return None


def tts_edge(trecho, vozes):
    import edge_tts
    pcm = b""
    with tempfile.TemporaryDirectory() as tmp:
        for k, l in enumerate(trecho):
            nome, fala = l.split(":", 1)
            v = vozes.get(nome.strip()) or next(iter(vozes.values()))
            arq = os.path.join(tmp, f"{k}.mp3")
            asyncio.run(edge_tts.Communicate(fala.strip(), v["edge"]).save(arq))
            r = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", arq, "-f", "s16le",
                                "-ar", str(TAXA), "-ac", "1", "-"], capture_output=True, check=True)
            pcm += r.stdout + silencio(0.25)
    return pcm


def voz_bloco(linhas, vozes):
    pcm = b""
    for n, trecho in enumerate(partir(linhas), 1):
        dados, origem = tts_gemini(trecho, vozes), "gemini"
        if dados is None:
            try:
                dados, origem = tts_edge(trecho, vozes), "edge"
            except Exception as e:
                print(f"  [aviso] edge: {e}")
                return None
        print(f"  Trecho {n}: {origem}, {len(dados) / TAXA / 2:.0f}s")
        pcm += dados + silencio(0.3)
    return pcm


def para_mp3(pcm, caminho):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le", "-ar", str(TAXA), "-ac", "1",
                    "-i", "-", "-b:a", "48k", caminho], input=pcm, check=True)


# ---------- principal ----------
def main():
    if not CHAVE:
        sys.exit("GEMINI_API_KEY ausente.")
    estado = json.load(open(ESTADO)) if os.path.exists(ESTADO) else {}
    agora = datetime.now(timezone.utc)
    corte = (agora - timedelta(hours=72)).isoformat()
    vistos = {h: t for h, t in estado.get("vistos", {}).items() if t >= corte}
    idx_clima = estado.get("clima_idx", 0)
    cidades = CFG["cidades"]

    pool = coletar()
    pid = agora.strftime("%Y%m%d-%H%M")
    pasta = os.path.join(DOCS, "programas", pid)
    os.makedirs(pasta, exist_ok=True)
    blocos, roteiros, titulos_bloco1 = [], [], []

    grade = CFG["grade"]
    for n, b in enumerate(grade, 1):
        print(f"Bloco {n}/{len(grade)}: {b['titulo']}")
        usados = []
        if b["tipo"] == "clima":
            material = []
            for _ in range(b["n_cidades"]):
                c = cidades[idx_clima % len(cidades)]
                idx_clima += 1
                try:
                    material.append(clima(c))
                except Exception as e:
                    print(f"  [aviso] clima {c['nome']}: {e}")
        elif b["tipo"] == "resumo":
            material = [f"- {t}" for t in titulos_bloco1[:b["n_itens"]]]
        else:
            usados = escolher(pool, b.get("categorias"), b["n_itens"], vistos)
            material = [linha_item(i) for i in usados]
            if n == 1:
                titulos_bloco1 = [i["titulo"] for i in usados]
        if len(material) < 2:
            print("  [aviso] material insuficiente; bloco ignorado.")
            continue
        linhas = roteiro(b, material, n == 1, bool(b.get("ultimo")))
        if not linhas:
            print("  [aviso] sem roteiro; bloco ignorado.")
            continue
        pcm = voz_bloco(linhas, b["vozes"])
        if not pcm:
            print("  [aviso] sem áudio; bloco ignorado.")
            continue
        arq = f"{len(blocos) + 1:02d}.mp3"
        para_mp3(pcm, os.path.join(pasta, arq))
        blocos.append({"titulo": b["titulo"], "arquivo": arq, "duracao": round(len(pcm) / TAXA / 2)})
        roteiros.append(f"## {b['titulo']}\n" + "\n".join(linhas))
        for i in usados:
            vistos[i["h"]] = agora.isoformat()

    if not blocos:
        shutil.rmtree(pasta, ignore_errors=True)
        sys.exit("Nenhum bloco foi gerado.")

    open(os.path.join(pasta, "roteiro.txt"), "w", encoding="utf-8").write("\n\n".join(roteiros))
    man_path = os.path.join(DOCS, "programas.json")
    man = json.load(open(man_path)) if os.path.exists(man_path) else {"programas": []}
    man["programas"].append({"id": pid, "criado": agora.isoformat(), "blocos": blocos})
    man["programas"] = man["programas"][-CFG["manter_programas"]:]
    manter = {p["id"] for p in man["programas"]}
    for d in os.listdir(os.path.join(DOCS, "programas")):
        if d not in manter:
            shutil.rmtree(os.path.join(DOCS, "programas", d), ignore_errors=True)
    json.dump(man, open(man_path, "w"), indent=1)
    json.dump({"vistos": vistos, "clima_idx": idx_clima % len(cidades)}, open(ESTADO, "w"))
    total = sum(b["duracao"] for b in blocos)
    print(f"Programa {pid}: {len(blocos)} blocos, {total // 60} min {total % 60} s")


if __name__ == "__main__":
    main()
