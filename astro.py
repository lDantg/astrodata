"""Astrocade Dashboard - coletor + servidor local.

Uso:
  python astro.py              -> abre a dashboard
                                  (modo nuvem: baixa as coletas do GitHub a cada 15 min;
                                   modo local: coleta aqui a cada INTERVALO_MIN)
  python astro.py coletar      -> faz uma coleta, grava em data/AAAA-MM-DD.jsonl e sai
                                  (é isso que o GitHub Actions roda)
"""
import glob, json, os, sqlite3, sys, threading, time, urllib.request, webbrowser
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# ---------------- configuração ----------------
MEU_USUARIO = "dantg"
OUTROS_PERFIS = []          # ex.: ["fulano", "ciclano"] para acompanhar outros criadores
GITHUB_REPO = "lDantg/astrodata"   # vazio ("") = coleta local no PC
GITHUB_TOKEN = ""           # só se o repositório for PRIVADO (token de leitura)
INTERVALO_MIN = 60          # modo local: minutos entre coletas
SYNC_MIN = 15               # modo nuvem: minutos entre downloads do GitHub
PORTA = 8765
SCROLLS_HOME = 8
# -----------------------------------------------

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "astrocade.db")
DATA = os.path.join(BASE, "data")
API = "astrocade-api-prod.com/v3"
NUVEM = bool(GITHUB_REPO.strip())


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, ts INTEGER UNIQUE);
    CREATE TABLE IF NOT EXISTS game_snap(run_id INT, game_id TEXT, title TEXT, username TEXT,
        plays INT, likes INT, comments INT, shares INT, thumb TEXT, created_at INT,
        PRIMARY KEY(run_id, game_id));
    CREATE TABLE IF NOT EXISTS feed_pos(run_id INT, category TEXT, pos INT, game_id TEXT);
    CREATE TABLE IF NOT EXISTS creator_snap(run_id INT, username TEXT, total_plays INT,
        followers INT, avatar TEXT, PRIMARY KEY(run_id, username));
    CREATE TABLE IF NOT EXISTS arquivos(nome TEXT PRIMARY KEY, sha TEXT);
    CREATE INDEX IF NOT EXISTS ix_gs ON game_snap(game_id, run_id);
    CREATE INDEX IF NOT EXISTS ix_cs ON creator_snap(username, run_id);
    """)
    return c


# ---------------- coleta ----------------
def _is_game(o):
    return isinstance(o, dict) and "gameId" in o and "plays" in o and "title" in o


def capturar():
    """Abre o site num navegador anônimo e devolve um snapshot (dict)."""
    from playwright.sync_api import sync_playwright
    games, creators, feed = {}, {}, []

    def add_game(g, cat=None):
        games[g["gameId"]] = g
        if g.get("username") and g.get("ownerTotalPlays") is not None:
            creators.setdefault(g["username"], [g.get("ownerTotalPlays"),
                                g.get("ownerFollowerCount"), g.get("avatarHeadshotUrl")])
        if cat:
            feed.append((cat, g["gameId"]))

    def on_resp(r):
        if API not in r.url or "client-events" in r.url:
            return
        try:
            data = r.json()
        except Exception:
            return
        u = urlparse(r.url)
        q = parse_qs(u.query)
        if u.path.endswith("/users") and isinstance(data, dict) and data.get("username"):
            creators[data["username"]] = [data.get("totalPlays"), data.get("followerCount"),
                                          data.get("profilePicture")]
            return
        if u.path.endswith("/feed") and isinstance(data, dict):
            by_cat = (data.get("feed_config") or {}).get("gamesByCategory") or {}
            for c, blk in by_cat.items():
                for g in blk.get("games", []):
                    if _is_game(g): add_game(g, c)
            cat = q.get("category", [None])[0]
            if cat:
                for g in data.get("games", []):
                    if _is_game(g): add_game(g, cat)
                return

        def walk(o):
            if _is_game(o): add_game(o)
            elif isinstance(o, dict): [walk(v) for v in o.values()]
            elif isinstance(o, list): [walk(v) for v in o]
        walk(data)

    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1400, "height": 1000})
        pg.on("response", on_resp)
        pg.goto("https://www.astrocade.com/", wait_until="load", timeout=60000)
        pg.wait_for_timeout(4000)
        for _ in range(SCROLLS_HOME):
            pg.mouse.wheel(0, 2500); pg.wait_for_timeout(1500)
        for u in [MEU_USUARIO] + OUTROS_PERFIS:
            pg.goto(f"https://www.astrocade.com/profile/{u}", wait_until="load", timeout=60000)
            pg.wait_for_timeout(4000)
            for _ in range(3):
                pg.mouse.wheel(0, 3000); pg.wait_for_timeout(1500)
        b.close()

    seen, pos, fl = set(), {}, []
    for cat, gid in feed:
        if (cat, gid) in seen: continue
        seen.add((cat, gid)); pos[cat] = pos.get(cat, 0) + 1
        fl.append([cat, pos[cat], gid])
    return {
        "ts": int(time.time()),
        "games": [[g["gameId"], g.get("title"), g.get("username"), g.get("plays"),
                   g.get("likeCount"), g.get("commentsCount"), g.get("shares"),
                   g.get("thumbnailUrl"), g.get("firstPublishedTimestamp") or g.get("createdAt")]
                  for g in games.values()],
        "feed": fl,
        "creators": [[u] + v for u, v in creators.items()],
    }


CORTE = os.path.join(BASE, "limpeza_local.txt")   # data de corte do botão "Limpar dados"


def ler_corte():
    try:
        with open(CORTE) as f: return int(f.read().strip() or 0)
    except Exception:
        return 0


def limpar():
    """Apaga o histórico local e passa a ignorar coletas anteriores a agora."""
    with open(CORTE, "w") as f: f.write(str(int(time.time())))
    c = db()
    for t in ("game_snap", "feed_pos", "creator_snap", "runs"):
        c.execute(f"DELETE FROM {t}")
    c.commit(); c.execute("VACUUM"); c.close()
    print(f"[{datetime.now():%d/%m %H:%M}] histórico local limpo")


def importar(c, snap):
    """Grava um snapshot no SQLite (ignora se aquele horário já existe ou é anterior à limpeza)."""
    if snap["ts"] < ler_corte():
        return False
    if c.execute("SELECT 1 FROM runs WHERE ts=?", (snap["ts"],)).fetchone():
        return False
    run = c.execute("INSERT INTO runs(ts) VALUES(?)", (snap["ts"],)).lastrowid
    c.executemany("INSERT OR REPLACE INTO game_snap VALUES(?,?,?,?,?,?,?,?,?,?)",
                  [[run] + g for g in snap["games"]])
    c.executemany("INSERT INTO feed_pos VALUES(?,?,?,?)", [[run] + f for f in snap["feed"]])
    c.executemany("INSERT OR REPLACE INTO creator_snap VALUES(?,?,?,?,?)",
                  [[run] + cr for cr in snap["creators"]])
    return True


def salvar_arquivo(snap):
    os.makedirs(DATA, exist_ok=True)
    dia = datetime.fromtimestamp(snap["ts"], timezone.utc).strftime("%Y-%m-%d")
    with open(os.path.join(DATA, f"{dia}.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(snap, ensure_ascii=False, separators=(",", ":")) + "\n")


def coletar():
    snap = capturar()
    salvar_arquivo(snap)
    c = db(); importar(c, snap); c.commit(); c.close()
    print(f"[{datetime.now():%d/%m %H:%M}] coleta: {len(snap['games'])} jogos, "
          f"{len(snap['creators'])} criadores, {len({f[0] for f in snap['feed']})} categorias")


def importar_pasta_local():
    """Importa os .jsonl da pasta data/ (útil depois de baixar ou copiar arquivos)."""
    c = db(); n = 0
    for fn in sorted(glob.glob(os.path.join(DATA, "*.jsonl"))):
        with open(fn, encoding="utf-8") as f:
            for line in f:
                if line.strip() and importar(c, json.loads(line)): n += 1
    c.commit(); c.close()
    return n


# ---------------- sincronização com o GitHub ----------------
def _get(url):
    h = {"User-Agent": "astrocade-dashboard", "Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN.strip():
        h["Authorization"] = f"Bearer {GITHUB_TOKEN.strip()}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=60) as r:
        return r.read()


def sincronizar():
    """Baixa do GitHub só os arquivos de data/ que mudaram e importa as coletas novas."""
    lista = json.loads(_get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/data"))
    c = db(); novos = 0
    conhecidos = {r["nome"]: r["sha"] for r in c.execute("SELECT * FROM arquivos")}
    for f in sorted(lista, key=lambda x: x["name"]):
        if not f["name"].endswith(".jsonl") or conhecidos.get(f["name"]) == f["sha"]:
            continue
        raw = _get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/data/{f['name']}")
        conteudo = json.loads(raw)
        import base64
        texto = base64.b64decode(conteudo["content"]).decode("utf-8") if conteudo.get("content") \
            else _get(conteudo["download_url"]).decode("utf-8")
        for line in texto.splitlines():
            if line.strip() and importar(c, json.loads(line)): novos += 1
        c.execute("INSERT OR REPLACE INTO arquivos VALUES(?,?)", (f["name"], f["sha"]))
    c.commit(); c.close()
    print(f"[{datetime.now():%d/%m %H:%M}] sincronizado com GitHub: {novos} coletas novas")


# ---------------- dados para a dashboard ----------------
def _delta(c, table, key, val, ids, horas):
    if not ids: return {}
    alvo = int(time.time()) - horas * 3600
    marks = ",".join("?" * len(ids))
    rows = c.execute(f"""
      SELECT t.{key} k, r.ts, t.{val} v FROM {table} t JOIN runs r ON r.id=t.run_id
      WHERE t.{key} IN ({marks}) ORDER BY r.ts""", list(ids)).fetchall()
    out = {}
    for r in rows:
        if r["v"] is None: continue
        o = out.setdefault(r["k"], {"base": None, "base_ts": None, "last": None, "last_ts": None})
        if o["base"] is None or r["ts"] <= alvo:
            o["base"], o["base_ts"] = r["v"], r["ts"]
        o["last"], o["last_ts"] = r["v"], r["ts"]
    for o in out.values():
        o["gain"] = (o["last"] or 0) - (o["base"] or 0)
        o["hours"] = round((o["last_ts"] - o["base_ts"]) / 3600, 1)
    return out


def dados(horas=24):
    c = db()
    last = c.execute("SELECT id, ts FROM runs ORDER BY ts DESC LIMIT 1").fetchone()
    if not last:
        return {"vazio": True, "nuvem": NUVEM}
    run, nruns = last["id"], c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    first_ts = c.execute("SELECT MIN(ts) FROM runs").fetchone()[0]
    janela = int(time.time()) - max(48, horas) * 3600

    recent = [r[0] for r in c.execute("""SELECT DISTINCT game_id FROM game_snap gs JOIN runs r
              ON r.id=gs.run_id WHERE r.ts > ?""", (janela,))]
    info = {r["game_id"]: dict(r) for r in c.execute("""
        SELECT gs.* FROM game_snap gs JOIN runs r ON r.id=gs.run_id
        JOIN (SELECT g.game_id, MAX(r2.ts) m FROM game_snap g JOIN runs r2 ON r2.id=g.run_id
              GROUP BY g.game_id) x ON x.game_id=gs.game_id AND x.m=r.ts""")}
    gd = _delta(c, "game_snap", "game_id", "plays", recent, horas)

    def g(gid, **extra):
        i = info.get(gid, {}); d = gd.get(gid, {})
        return {"id": gid, "title": i.get("title"), "user": i.get("username"),
                "plays": i.get("plays"), "likes": i.get("likes"), "thumb": i.get("thumb"),
                "gain": d.get("gain", 0), "hours": d.get("hours", 0), **extra}

    trending = [g(r["game_id"], pos=r["pos"]) for r in c.execute(
        "SELECT * FROM feed_pos WHERE run_id=? AND category='trending' ORDER BY pos", (run,))]
    cats = {}
    for r in c.execute("SELECT category, game_id, pos FROM feed_pos WHERE run_id=? ORDER BY pos", (run,)):
        cats.setdefault(r["category"], []).append(g(r["game_id"], pos=r["pos"]))
    top_games = sorted((g(x) for x in recent), key=lambda x: x["gain"], reverse=True)[:25]

    users = [r[0] for r in c.execute("""SELECT DISTINCT username FROM creator_snap cs JOIN runs r
             ON r.id=cs.run_id WHERE r.ts > ?""", (janela,))]
    cd = _delta(c, "creator_snap", "username", "total_plays", users, horas)
    cinfo = {r["username"]: dict(r) for r in c.execute("""
        SELECT cs.* FROM creator_snap cs JOIN runs r ON r.id=cs.run_id
        JOIN (SELECT s.username, MAX(r2.ts) m FROM creator_snap s JOIN runs r2 ON r2.id=s.run_id
              GROUP BY s.username) x ON x.username=cs.username AND x.m=r.ts""")}
    top_creators = sorted(({"user": u, "total": cinfo[u]["total_plays"],
                            "followers": cinfo[u]["followers"], "avatar": cinfo[u]["avatar"],
                            "gain": cd.get(u, {}).get("gain", 0)}
                           for u in users if u in cinfo),
                          key=lambda x: (x["gain"], x["total"] or 0), reverse=True)[:25]

    mine_ids = [r[0] for r in c.execute(
        "SELECT DISTINCT game_id FROM game_snap WHERE username=?", (MEU_USUARIO,))]
    md = _delta(c, "game_snap", "game_id", "plays", mine_ids, horas)
    in_feed = {}
    if mine_ids:
        for r in c.execute(f"""SELECT game_id, category, pos FROM feed_pos WHERE run_id=? AND game_id IN
                          ({','.join('?' * len(mine_ids))})""", [run] + mine_ids):
            in_feed.setdefault(r["game_id"], []).append(f'{r["category"]} #{r["pos"]}')
    mine = []
    for gid in mine_ids:
        i = info[gid]
        serie = [[r["ts"], r["plays"]] for r in c.execute("""SELECT r.ts, gs.plays FROM game_snap gs
                 JOIN runs r ON r.id=gs.run_id WHERE gs.game_id=? ORDER BY r.ts""", (gid,))]
        mine.append({"id": gid, "title": i["title"], "plays": i["plays"], "likes": i["likes"],
                     "comments": i["comments"], "shares": i["shares"], "thumb": i["thumb"],
                     "gain": md.get(gid, {}).get("gain", 0), "feed": in_feed.get(gid, []),
                     "serie": serie})
    mine.sort(key=lambda x: x["gain"], reverse=True)

    total_serie = [[r["ts"], r["total_plays"]] for r in c.execute("""SELECT r.ts, cs.total_plays
        FROM creator_snap cs JOIN runs r ON r.id=cs.run_id WHERE cs.username=? ORDER BY r.ts""",
        (MEU_USUARIO,))]
    me = cinfo.get(MEU_USUARIO, {})
    c.close()
    return {"user": MEU_USUARIO, "last_ts": last["ts"], "first_ts": first_ts, "runs": nruns,
            "horas": horas, "intervalo": INTERVALO_MIN, "nuvem": NUVEM, "repo": GITHUB_REPO,
            "me": {"total": me.get("total_plays"), "followers": me.get("followers"),
                   "avatar": me.get("avatar"), "gain": cd.get(MEU_USUARIO, {}).get("gain", 0),
                   "serie": total_serie},
            "trending": trending, "categorias": cats, "top_games": top_games,
            "top_creators": top_creators, "mine": mine}


# ---------------- servidor ----------------
status = {"coletando": False, "erro": None}


def atualizar_seguro():
    """Modo nuvem: baixa do GitHub. Modo local: coleta aqui."""
    if status["coletando"]: return
    status["coletando"] = True
    try:
        sincronizar() if NUVEM else coletar()
        status["erro"] = None
    except Exception as e:
        status["erro"] = str(e)[:200]; print("ERRO:", e)
    finally:
        status["coletando"] = False


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=BASE, **k)
    def log_message(self, *a): pass

    def _json(self, o):
        b = json.dumps(o, ensure_ascii=False).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/dados":
            h = int(parse_qs(u.query).get("horas", ["24"])[0])
            return self._json({**dados(h), "status": status})
        if u.path == "/api/limpar":
            limpar()
            return self._json({"ok": True})
        if u.path == "/api/coletar":
            threading.Thread(target=atualizar_seguro, daemon=True).start()
            return self._json({"ok": True})
        if u.path in ("/", ""):
            self.path = "/dashboard.html"
        if self.path.split("?")[0] != "/dashboard.html":
            return self.send_error(404)
        return super().do_GET()


def loop():
    while True:
        atualizar_seguro()
        time.sleep((SYNC_MIN if NUVEM else INTERVALO_MIN) * 60)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "coletar":
        coletar(); sys.exit()
    if len(sys.argv) > 1 and sys.argv[1] == "importar":
        print(importar_pasta_local(), "coletas importadas"); sys.exit()
    threading.Thread(target=loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORTA), H)
    modo = f"baixando do GitHub ({GITHUB_REPO}) a cada {SYNC_MIN} min" if NUVEM \
        else f"coletando aqui a cada {INTERVALO_MIN} min"
    print(f"Dashboard em http://localhost:{PORTA}  ({modo}; Ctrl+C para sair)")
    webbrowser.open(f"http://localhost:{PORTA}")
    srv.serve_forever()
