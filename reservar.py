"""Vigia um comboio da CP e reserva com o Passe Ferroviario Verde quando houver lugar.

Dados pessoais vem SEMPRE de variaveis de ambiente (segredos do GitHub).
Nunca escrever dados pessoais nos prints: os registos do GitHub Actions sao publicos.

Modos:
- teste: faz UMA verificacao, nunca confirma a reserva e avisa o resultado.
- real: vigia de 5 em 5 minutos (a partir de 25h antes da partida) e reserva ao primeiro lugar.
"""
import json
import os
import random
import re
import time
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

TZ = ZoneInfo("Europe/Lisbon")
DIAS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
        "sexta-feira", "sábado", "domingo"]
MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
         "agosto", "setembro", "outubro", "novembro", "dezembro"]

JANELA_H = 24                   # comecar a vigiar 24h antes da partida (so ai o passe verde se aplica)
LIMITE_S = 5 * 3600 + 15 * 60   # duracao maxima de cada execucao (o GitHub corta as 6h)
INTERVALO_S = 300               # 5 minutos entre verificacoes

# nome -> (texto a escrever na pesquisa, texto da sugestao a escolher)
ESTACOES = {
    "Lisboa": ("Lisboa", "Lisboa Santa Apolonia"),
    "Coimbra": ("Coimbra", "Coimbra-B"),
}
RELATIVAS = {"hoje": 0, "amanhã": 1, "amanha": 1,
             "depois de amanhã": 2, "depois de amanha": 2}


def notificar(msg: str) -> None:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("(sem NTFY_TOPIC definido)")
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=msg.encode("utf-8"),
            headers={"Title": "CP reserva"},
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:  # nao falhar por causa da notificacao
        print(f"Falha ao notificar: {type(e).__name__}")


def set_output(chave: str, valor: str) -> None:
    caminho = os.environ.get("GITHUB_OUTPUT")
    if caminho:
        with open(caminho, "a", encoding="utf-8") as f:
            f.write(f"{chave}={valor}\n")


def carregar_config() -> dict:
    """Le a viagem do formulario do GitHub (variaveis TRIP_*) ou, no PC, do viagem.json."""
    sentido = os.environ.get("TRIP_SENTIDO")
    if not sentido:
        with open("viagem.json", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["continuacao"] = False
        return cfg

    partes = [s.strip() for s in sentido.split(" para ")]
    if len(partes) != 2 or partes[0] not in ESTACOES or partes[1] not in ESTACOES:
        raise ValueError("sentido")
    origem, destino = ESTACOES[partes[0]], ESTACOES[partes[1]]

    exata = os.environ.get("TRIP_DATA_EXATA", "").strip()
    if exata:
        dia = datetime.strptime(exata, "%Y-%m-%d").date()
    else:
        rel = RELATIVAS[os.environ.get("TRIP_DATA", "amanhã").strip().lower()]
        dia = datetime.now(TZ).date() + timedelta(days=rel)

    m = re.fullmatch(r"(\d{1,2})[:h.](\d{2})", os.environ.get("TRIP_HORA", "").strip())
    if not m:
        raise ValueError("hora")
    hora = f"{int(m.group(1)):02d}:{m.group(2)}"

    return {
        "origem_pesquisa": origem[0], "origem_opcao": origem[1],
        "destino_pesquisa": destino[0], "destino_opcao": destino[1],
        "data": dia.strftime("%Y-%m-%d"),
        "hora": hora,
        "dry_run": os.environ.get("TRIP_TESTE", "true").strip().lower() == "true",
        "continuacao": os.environ.get("TRIP_CONTINUACAO", "false").strip().lower() == "true",
    }


def login(page) -> None:
    email = os.environ["CP_EMAIL"]
    password = os.environ["CP_PASSWORD"]
    page.goto("https://www.cp.pt/pt")
    try:
        page.get_by_role("button", name="Rejeitar Todos").click(timeout=5000)
    except PWTimeout:
        pass
    page.get_by_role("button", name="myCP profile").click()
    page.get_by_role("textbox", name="Email").fill(email)
    page.get_by_role("textbox", name="Palavra-passe").fill(password)
    page.get_by_role("button", name="Entrar agora").click()
    # O site redireciona sozinho depois do login: esperar, nao forcar a navegacao.
    try:
        page.wait_for_url(re.compile(r"/mycp"), timeout=30000)
    except (PWTimeout, PWError):
        page.goto("https://www.cp.pt/pt/mycp")


def ir_para_pesquisa(page) -> None:
    if "/mycp" not in page.url:
        page.goto("https://www.cp.pt/pt/mycp")
    page.get_by_role("tabpanel", name="Passageiros").get_by_role("link", name="Início").click()


def escolher_estacao(page, campo: str, pesquisa: str, opcao: str) -> None:
    """Escreve letra a letra (para a lista de sugestoes aparecer) e escolhe a opcao."""
    caixa = page.get_by_role("textbox", name=campo)
    caixa.wait_for(state="visible", timeout=30000)
    caixa.click()
    caixa.fill("")
    caixa.press_sequentially(pesquisa, delay=150)
    sugestao = page.get_by_text(opcao).first
    try:
        sugestao.wait_for(state="visible", timeout=8000)
    except PWTimeout:
        caixa.fill("")
        caixa.press_sequentially(pesquisa, delay=250)
        sugestao.wait_for(state="visible", timeout=12000)
    sugestao.click()


def escolher_data(page, data: datetime) -> None:
    dia_semana = DIAS[data.weekday()]
    padrao = re.compile(
        rf"Choose {re.escape(dia_semana)}, {data.day} de {MESES[data.month - 1]}"
    )
    page.get_by_role("textbox", name="Data", exact=True).click()
    celula = page.get_by_role("gridcell", name=padrao)
    if celula.count() == 0:
        # melhor esforco: avancar para o mes seguinte no calendario
        try:
            page.get_by_role("button", name=re.compile("next|seguinte", re.I)).first.click(timeout=3000)
        except PWTimeout:
            pass
    celula.first.click(timeout=8000)


def verificar(page, cfg: dict, data: datetime, resumo: str) -> bool:
    """True se ha lugar (e, em modo real, a reserva foi submetida)."""
    info = os.environ["CP_INFO_ADICIONAL"]

    escolher_estacao(page, "Origem *", cfg["origem_pesquisa"], cfg["origem_opcao"])
    escolher_estacao(page, "Destino *", cfg["destino_pesquisa"], cfg["destino_opcao"])
    escolher_data(page, data)
    page.get_by_role("button", name="Pesquisar viagens").click()

    selecionar = page.get_by_role(
        "button", name=re.compile(rf"Selecionar ida das {re.escape(cfg['hora'])}")
    )
    try:
        selecionar.first.wait_for(state="visible", timeout=15000)
    except PWTimeout:
        print("Comboio nao listado.")
        return False
    if not selecionar.first.is_enabled():
        print("Comboio sem lugar (botao desativado).")
        return False
    selecionar.first.click()

    comprar = page.get_by_role("button", name=re.compile("Comprar esta viagem"))
    try:
        comprar.first.wait_for(state="visible", timeout=8000)
    except PWTimeout:
        print("Sem opcao de compra: sem lugar.")
        return False
    comprar.first.click()

    page.locator(".custom-checkbox").click()
    page.get_by_role("checkbox", name="Confirmo que: Li e aceito as").check()
    page.get_by_role("button", name="Avançar para compra").click()

    # Num comboio esgotado a pagina de compra nao tem o campo "Desconto".
    desconto = page.get_by_role("combobox", name="Desconto *")
    try:
        desconto.wait_for(state="visible", timeout=12000)
    except PWTimeout:
        print("Sem campo de desconto: sem lugar (ou passe ainda nao disponivel).")
        return False
    desconto.click()
    page.get_by_role("option", name="Passe Ferroviário Verde").click()
    page.get_by_role("textbox", name="Informação adicional *").fill(info)
    page.get_by_role("button", name="Avançar").click()
    page.get_by_role("button", name="Avançar").click()
    page.get_by_role("button", name="Avançar para pagamento").click()

    if cfg.get("dry_run", True):
        notificar(f"TESTE: ha lugar ({resumo}). Parei antes de confirmar.")
        return True

    # A partir daqui NUNCA repetir: qualquer erro depois do clique nao pode causar uma 2a reserva.
    try:
        page.get_by_role("button", name="Confirmar").click()
    except Exception as e:
        notificar(f"Erro ao confirmar ({resumo}, {type(e).__name__}). Verifica na app da CP; parei para nao duplicar.")
        return True
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except (PWTimeout, PWError):
        pass
    # Dar tempo ao pedido de compra para terminar antes de o browser fechar.
    time.sleep(20)
    if os.environ.get("LOCAL"):
        try:
            page.screenshot(path="apos_confirmar.png", full_page=True)
            print("Pagina depois de Confirmar:", page.url.split("?")[0])
            botoes = [b.inner_text().strip().replace("\n", " ")[:40]
                      for b in page.get_by_role("button").all() if b.is_visible()]
            print("Botoes visiveis:", botoes)
        except Exception as e:
            print("Diagnostico falhou:", type(e).__name__)
    notificar(f"Reserva feita ({resumo}). Confirma na app da CP que o bilhete apareceu.")
    return True


def vigiar(page, cfg: dict, data: datetime, inicio: float, resumo: str) -> str:
    teste = cfg.get("dry_run", True)
    logado = False
    falhas = 0
    while True:
        if datetime.now(TZ) >= data:
            notificar(f"O comboio ({resumo}) ja partiu; parei de vigiar.")
            return "partiu"
        if time.time() - inicio + INTERVALO_S > LIMITE_S:
            return "continuar"
        try:
            if not logado:
                login(page)
                logado = True
            ir_para_pesquisa(page)
            ha_lugar = verificar(page, cfg, data, resumo)
        except Exception as e:
            falhas += 1
            logado = False
            print(f"Falha {falhas}: {type(e).__name__}")
            if teste and falhas >= 3:
                raise
            # avisar so na 3a falha seguida e depois de hora a hora; nunca desistir
            if falhas == 3 or (falhas > 3 and falhas % 12 == 0):
                notificar(f"Tenho tido erros ({type(e).__name__}) a verificar {resumo}. Continuo a tentar.")
            time.sleep(60 if falhas < 3 else INTERVALO_S)
            continue
        if falhas >= 3:
            notificar(f"Voltou a funcionar ({resumo}). Continuo a vigiar.")
        falhas = 0
        if ha_lugar:
            return "feito"
        if teste:
            notificar(f"TESTE: sem lugar ({resumo}).")
            return "feito"
        time.sleep(INTERVALO_S + random.randint(0, 60))


def main() -> None:
    inicio = time.time()
    try:
        cfg = carregar_config()
        data = datetime.strptime(f"{cfg['data']} {cfg['hora']}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
    except (ValueError, KeyError) as e:
        notificar(f"Dados da viagem invalidos ({type(e).__name__}). Verifica o formulario.")
        set_output("continuar", "false")
        return

    set_output("data_final", cfg["data"])
    set_output("hora_final", cfg["hora"])
    resumo = f"{cfg['origem_opcao']} > {cfg['destino_opcao']}, {cfg['data']} {cfg['hora']}"
    teste = cfg.get("dry_run", True)

    if datetime.now(TZ) >= data:
        notificar(f"O comboio ({resumo}) ja partiu; nada a fazer.")
        set_output("continuar", "false")
        return

    if not cfg.get("continuacao"):
        notificar(f"TESTE: a verificar {resumo}." if teste else f"A vigiar {resumo}.")

    if not teste:
        # so comecar a vigiar quando a janela das 24h estiver perto
        while True:
            faltam = (data - datetime.now(TZ)) - timedelta(hours=JANELA_H)
            if faltam <= timedelta(0):
                break
            pausa = min(faltam.total_seconds(), 1800)
            if time.time() - inicio + pausa > LIMITE_S:
                set_output("continuar", "true")
                return
            time.sleep(pausa)

    with sync_playwright() as p:
        if os.environ.get("LOCAL"):
            browser = p.chromium.launch(channel="chrome", headless=False, slow_mo=400)
        else:
            browser = p.chromium.launch(headless=True, slow_mo=300)
        context = browser.new_context()
        context.set_default_timeout(20000)
        page = context.new_page()
        try:
            resultado = vigiar(page, cfg, data, inicio, resumo)
        except Exception as e:
            notificar(f"Erro ({type(e).__name__}): a vigilancia parou. Ve os registos no GitHub.")
            if os.environ.get("LOCAL"):
                try:
                    page.screenshot(path="erro.png")
                except Exception:
                    pass
            raise
        finally:
            context.close()
            browser.close()

    set_output("continuar", "true" if resultado == "continuar" else "false")


if __name__ == "__main__":
    main()
