"""Captura de dados do Astrocade com navegador anônimo (Playwright).

Fluxo de cada coleta:
  1. Páginas de categoria (trending, recommended_for_you, top_picks): lista completa e posições.
  2. Home: categorias por gênero.
  3. Perfis (o seu + OUTROS_PERFIS).
  4. Jogos rastreados: todo jogo que já apareceu em alguma das 3 categorias fica salvo em
     data/rastreados.json. Os que não apareceram nas listas desta vez são abertos
     individualmente para atualizar os plays.
"""
import asyncio, json, os, re, time
from urllib.parse import urlparse, parse_qs

API = "astrocade-api-prod.com/v3"
SITE = "https://www.astrocade.com"
CATEGORIAS = ["trending", "recommended_for_you", "top_picks"]
PARALELO = 5                 # abas abertas ao mesmo tempo nas visitas individuais
MAX_INDIVIDUAIS = 250        # limite de jogos abertos individualmente por coleta
BLOQUEAR = {"image", "media", "font"}


def _is_game(o):
    return isinstance(o, dict) and "gameId" in o and "plays" in o and "title" in o


def _walk(o, f):
    if _is_game(o): f(o)
    if isinstance(o, dict):
        for v in o.values(): _walk(v, f)
    elif isinstance(o, list):
        for v in o: _walk(v, f)


def _jogos_no_html(html):
    """Os primeiros jogos das páginas de categoria vêm embutidos no HTML (payload do Next.js)."""
    out = []
    dec = json.JSONDecoder()
    for chunk in re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', html):
        try: txt = json.loads('"' + chunk + '"')
        except Exception: continue
        i = 0
        while True:
            i = txt.find('{"gameId"', i)
            if i < 0: break
            try:
                o, end = dec.raw_decode(txt, i)
                _walk(o, out.append); i = end
            except Exception:
                i += 9
    return out


class Coleta:
    def __init__(self, usuario, outros):
        self.perfis = [usuario] + list(outros)
        self.games, self.creators, self.feed = {}, {}, []
        self.hrefs = {}

    def add(self, g, cat=None):
        self.games[g["gameId"]] = g
        if g.get("username") and g.get("ownerTotalPlays") is not None:
            self.creators.setdefault(g["username"], [g.get("ownerTotalPlays"),
                                     g.get("ownerFollowerCount"), g.get("avatarHeadshotUrl")])
        if cat: self.feed.append((cat, g["gameId"]))

    async def on_resp(self, r):
        if API not in r.url or "client-events" in r.url or "entryGameId=" in r.url: return  # ignora "relacionados"
        try: data = await r.json()
        except Exception: return
        u = urlparse(r.url)
        if u.path.endswith("/users") and isinstance(data, dict) and data.get("username"):
            self.creators[data["username"]] = [data.get("totalPlays"), data.get("followerCount"),
                                               data.get("profilePicture")]
            return
        if u.path.endswith("/feed") and isinstance(data, dict):
            for c, blk in ((data.get("feed_config") or {}).get("gamesByCategory") or {}).items():
                for g in blk.get("games", []):
                    if _is_game(g): self.add(g, "home:" + c)
        _walk(data, self.add)

    async def nova_aba(self, ctx):
        pg = await ctx.new_page()
        await pg.route("**/*", lambda rt: rt.abort() if rt.request.resource_type in BLOQUEAR
                       else rt.continue_())
        pg.on("response", lambda r: asyncio.ensure_future(self.on_resp(r)))
        return pg

    async def rolar(self, pg, max_vezes=15):
        last, parado = -1, 0
        for _ in range(max_vezes):
            await pg.evaluate("()=>{const m=document.querySelector('main');if(m)m.scrollTop=m.scrollHeight;"
                              "window.scrollTo(0,document.body.scrollHeight)}")
            await pg.wait_for_timeout(1200)
            n = await pg.eval_on_selector_all('a[href*="/games/"]', "e=>e.length")
            parado = parado + 1 if n == last else 0
            last = n
            if parado >= 3: break

    async def categoria(self, ctx, cat):
        pg = await self.nova_aba(ctx)
        try:
            await pg.goto(f"{SITE}/category/{cat}", wait_until="load", timeout=60000)
            await pg.wait_for_timeout(2500)
            for g in _jogos_no_html(await pg.content()): self.add(g)
            await self.rolar(pg)
            await pg.wait_for_timeout(1000)
            hrefs = await pg.eval_on_selector_all('main a[href*="/games/"]', "e=>e.map(x=>x.getAttribute('href'))")
            ordem = []
            for h in hrefs:
                gid = h.rstrip("/").split("/")[-1]
                if re.fullmatch(r"[0-9A-Z]{26}", gid) and gid not in ordem:
                    ordem.append(gid); self.hrefs[gid] = h
            for gid in ordem: self.feed.append((cat, gid))
            return ordem
        finally:
            await pg.close()

    async def home(self, ctx):
        pg = await self.nova_aba(ctx)
        try:
            await pg.goto(SITE + "/", wait_until="load", timeout=60000)
            await pg.wait_for_timeout(2500)
            await self.rolar(pg, 8)
        finally:
            await pg.close()

    async def perfil(self, ctx, u):
        pg = await self.nova_aba(ctx)
        try:
            await pg.goto(f"{SITE}/profile/{u}", wait_until="load", timeout=60000)
            await pg.wait_for_timeout(2500)
            await self.rolar(pg, 5)
        finally:
            await pg.close()

    async def jogo(self, ctx, href, sem):
        async with sem:
            pg = await self.nova_aba(ctx)
            try:
                async with pg.expect_response(lambda r: "/v3/games?" in r.url and "gameId=" in r.url,
                                              timeout=25000):
                    await pg.goto(SITE + href, wait_until="domcontentloaded", timeout=40000)
                await pg.wait_for_timeout(500)
            except Exception:
                pass
            finally:
                await pg.close()


def _slug(t):
    s = re.sub(r"[^a-z0-9]+", "-", (t or "game").lower()).strip("-")
    return s or "game"


async def _capturar(usuario, outros, rastreados):
    from playwright.async_api import async_playwright
    col = Coleta(usuario, outros)
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={"width": 1400, "height": 1000})
        listas = {}
        for cat in CATEGORIAS:
            try: listas[cat] = await col.categoria(ctx, cat)
            except Exception as e: print("falha na categoria", cat, e); listas[cat] = []
        for tarefa in [col.home(ctx)] + [col.perfil(ctx, u) for u in col.perfis]:
            try: await tarefa
            except Exception as e: print("falha:", e)

        # registra novos rastreados
        agora = int(time.time())
        for cat, ids in listas.items():
            for pos, gid in enumerate(ids, 1):
                g = col.games.get(gid, {})
                r = rastreados.setdefault(gid, {"first_seen": agora, "cats": []})
                if cat not in r["cats"]: r["cats"].append(cat)
                r["last_listed"] = agora
                r["href"] = col.hrefs.get(gid) or r.get("href")
                if g: r["title"], r["user"] = g.get("title"), g.get("username")

        # visita individual dos rastreados que não vieram nesta coleta
        faltando = [gid for gid in rastreados if gid not in col.games]
        faltando.sort(key=lambda gid: rastreados[gid].get("last_checked", 0))
        faltando = faltando[:MAX_INDIVIDUAIS]
        sem = asyncio.Semaphore(PARALELO)
        await asyncio.gather(*[col.jogo(ctx, rastreados[gid].get("href")
                                        or f"/games/{_slug(rastreados[gid].get('title'))}/{gid}", sem)
                               for gid in faltando])
        for gid in rastreados:
            if gid in col.games:
                rastreados[gid]["last_checked"] = agora
                g = col.games[gid]
                rastreados[gid]["title"], rastreados[gid]["user"] = g.get("title"), g.get("username")
        await b.close()
    print(f"  categorias: " + ", ".join(f"{c}={len(v)}" for c, v in listas.items())
          + f" | rastreados: {len(rastreados)} | visitados individualmente: {len(faltando)}"
          + f" | sem resposta: {sum(1 for g in faltando if g not in col.games)}")
    return col


def _ler(arq, padrao):
    try:
        with open(arq, encoding="utf-8") as f: return json.load(f)
    except Exception:
        return padrao


def _gravar(arq, obj):
    os.makedirs(os.path.dirname(arq), exist_ok=True)
    with open(arq, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=0, sort_keys=True)


def capturar(usuario, outros, pasta):
    """Devolve um snapshot compacto (v2). Títulos, capas e avatares ficam em data/jogos.json."""
    arq_r, arq_m = os.path.join(pasta, "rastreados.json"), os.path.join(pasta, "jogos.json")
    rastreados = _ler(arq_r, {})
    col = asyncio.run(_capturar(usuario, outros, rastreados))
    _gravar(arq_r, rastreados)
    meta = _ler(arq_m, {"jogos": {}, "avatares": {}})
    for g in col.games.values():
        meta["jogos"][g["gameId"]] = [g.get("title"), g.get("username"), g.get("thumbnailUrl"),
                                      g.get("firstPublishedTimestamp") or g.get("createdAt")]
    for u, (_, _, av) in col.creators.items():
        if av: meta["avatares"][u] = av
    _gravar(arq_m, meta)

    seen, pos, fl = set(), {}, []
    for cat, gid in col.feed:
        if (cat, gid) in seen: continue
        seen.add((cat, gid)); pos[cat] = pos.get(cat, 0) + 1
        fl.append([cat, pos[cat], gid])
    return {
        "v": 2,
        "ts": int(time.time()),
        # [id, plays, likes, comentários, shares]
        "games": [[g["gameId"], g.get("plays"), g.get("likeCount"), g.get("commentsCount"),
                   g.get("shares")] for g in col.games.values()],
        "feed": fl,
        "creators": [[u, v[0], v[1]] for u, v in col.creators.items()],   # [user, total, seguidores]
    }
