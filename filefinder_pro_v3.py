"""
FileFinder Pro v3 — Moteur de recherche de fichiers
Fonctionnalités : Indexation SQLite/FTS5, Aperçu, Historique,
                  Favoris, Statistiques disque, Filtres avancés, Thèmes
"""

import os, sys, threading, subprocess, sqlite3, json, time, shutil
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import updater   # ← module de mise à jour automatique

# ═══════════════════════════════════════════════════════════════════════════════
#  THÈMES
# ═══════════════════════════════════════════════════════════════════════════════
THEMES = {
    "Sombre": {
        "BG": "#0f0f17", "PANEL": "#16161f", "CARD": "#1c1c28",
        "BORDER": "#2a2a3a", "ACCENT": "#6c63ff", "ACCENT_LT": "#9d97ff",
        "ACCENT_DIM": "#3d3880", "GREEN": "#00e5a0", "YELLOW": "#f5c542",
        "RED": "#ff5f5f", "TEXT": "#dde3f0", "TEXT_DIM": "#6b7280",
        "ENTRY_BG": "#0a0a12", "ROW_A": "#13131e", "ROW_B": "#17172a",
        "SEL": "#2e2b6e",
    },
    "Clair": {
        "BG": "#f0f2f8", "PANEL": "#ffffff", "CARD": "#e8eaf2",
        "BORDER": "#c8ccd8", "ACCENT": "#5b52e8", "ACCENT_LT": "#7c75f0",
        "ACCENT_DIM": "#b8b4f5", "GREEN": "#00a870", "YELLOW": "#d4a010",
        "RED": "#e03030", "TEXT": "#1a1a2e", "TEXT_DIM": "#555577",
        "ENTRY_BG": "#ffffff", "ROW_A": "#f5f6fc", "ROW_B": "#eceef8",
        "SEL": "#c5c2f8",
    },
}

T = THEMES["Sombre"].copy()   # thème actif (modifié à la volée)

DB_PATH   = os.path.join(os.path.expanduser("~"), ".filefinder_pro_v3.db")
CONF_PATH = os.path.join(os.path.expanduser("~"), ".filefinder_pro.json")
CHUNK     = 512
PREVIEW_EXTS = {".txt", ".py", ".js", ".ts", ".css", ".html", ".xml",
                ".json", ".csv", ".md", ".log", ".ini", ".cfg", ".bat",
                ".sh", ".yaml", ".yml", ".toml", ".sql", ".c", ".cpp",
                ".h", ".java", ".rs", ".go", ".rb", ".php"}
IMG_EXTS  = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}

# ═══════════════════════════════════════════════════════════════════════════════
#  BASE DE DONNÉES
# ═══════════════════════════════════════════════════════════════════════════════
class IndexDB:
    def __init__(self, path=DB_PATH):
        self.path  = path
        self._local = threading.local()
        self._init_schema()

    def _conn(self):
        if not hasattr(self._local, "conn") or self._local.conn is None:
            c = sqlite3.connect(self.path, check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA cache_size=-32000")
            c.execute("PRAGMA temp_store=MEMORY")
            self._local.conn = c
        return self._local.conn

    def _init_schema(self):
        c = self._conn()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                ext TEXT, dir TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
                size INTEGER DEFAULT 0, mtime REAL DEFAULT 0,
                indexed REAL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_ext   ON files(ext);
            CREATE INDEX IF NOT EXISTS idx_size  ON files(size);
            CREATE INDEX IF NOT EXISTS idx_mtime ON files(mtime);

            CREATE VIRTUAL TABLE IF NOT EXISTS files_fts
            USING fts5(name, path, content='files', content_rowid='id',
                       tokenize='unicode61 remove_diacritics 1');

            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL, ts REAL NOT NULL,
                results INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS favorites (
                path TEXT PRIMARY KEY, name TEXT, added REAL
            );
        """)
        c.commit()

    # ── Stats ──
    def stats(self):
        c = self._conn()
        row  = c.execute("SELECT COUNT(*), SUM(size) FROM files").fetchone()
        last = c.execute("SELECT value FROM meta WHERE key='last_index'").fetchone()
        roots= c.execute("SELECT value FROM meta WHERE key='roots'").fetchone()
        return {"count": row[0] or 0, "total_size": row[1] or 0,
                "last_index": last[0] if last else None,
                "roots": json.loads(roots[0]) if roots else []}

    # ── Indexation ──
    def rebuild(self, roots, progress_cb=None, stop_flag=None):
        c = self._conn()
        c.execute("DELETE FROM files"); c.execute("DELETE FROM files_fts"); c.commit()
        buf, total = [], 0
        ts = time.time()
        SKIP = {'$Recycle.Bin','System Volume Information','Windows','Recovery',
                'WindowsApps','ProgramData'}
        for root in roots:
            for dirpath, dirs, files in os.walk(root, followlinks=False):
                if stop_flag and stop_flag.is_set(): break
                dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith('$')]
                if progress_cb: progress_cb(total, dirpath)
                for fname in files:
                    full = os.path.join(dirpath, fname)
                    try: st = os.stat(full)
                    except: continue
                    buf.append((fname, os.path.splitext(fname)[1].lower(),
                                dirpath, full, st.st_size, st.st_mtime, ts))
                    total += 1
                    if len(buf) >= CHUNK: self._flush(c, buf); buf.clear()
        if buf: self._flush(c, buf)
        c.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")
        c.execute("REPLACE INTO meta VALUES('last_index',?)",
                  (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
        c.execute("REPLACE INTO meta VALUES('roots',?)", (json.dumps(roots),))
        c.commit()
        return total

    def update(self, roots, progress_cb=None, stop_flag=None):
        c = self._conn()
        ts = time.time()
        dead = [r[0] for r in c.execute("SELECT path FROM files")
                if not os.path.exists(r[0])]
        if dead:
            c.executemany("DELETE FROM files WHERE path=?", [(p,) for p in dead])
        buf, added = [], 0
        SKIP = {'$Recycle.Bin','System Volume Information','Windows','Recovery'}
        for root in roots:
            for dirpath, dirs, files in os.walk(root, followlinks=False):
                if stop_flag and stop_flag.is_set(): break
                dirs[:] = [d for d in dirs if d not in SKIP and not d.startswith('$')]
                if progress_cb: progress_cb(added, dirpath)
                for fname in files:
                    full = os.path.join(dirpath, fname)
                    try: st = os.stat(full)
                    except: continue
                    row = c.execute("SELECT mtime FROM files WHERE path=?",
                                    (full,)).fetchone()
                    if row is None or abs(row[0]-st.st_mtime) > 1:
                        buf.append((fname, os.path.splitext(fname)[1].lower(),
                                    dirpath, full, st.st_size, st.st_mtime, ts))
                        added += 1
                        if len(buf) >= CHUNK: self._flush(c, buf); buf.clear()
        if buf: self._flush(c, buf)
        c.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")
        c.execute("REPLACE INTO meta VALUES('last_index',?)",
                  (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
        c.commit()
        return added, len(dead)

    def _flush(self, conn, buf):
        conn.executemany(
            "INSERT OR REPLACE INTO files(name,ext,dir,path,size,mtime,indexed)"
            " VALUES(?,?,?,?,?,?,?)", buf)
        conn.commit()

    # ── Recherche ──
    def search(self, query, ext=None, min_size=None, max_size=None,
               date_from=None, date_to=None, limit=50000):
        c = self._conn()
        params = []
        if len(query) >= 3:
            safe = query.replace('"','""')
            sql  = ('SELECT f.name,f.ext,f.size,f.mtime,f.path '
                    'FROM files_fts ft JOIN files f ON f.id=ft.rowid '
                    'WHERE files_fts MATCH ?')
            params.append(f'"{safe}"*')
        else:
            sql  = 'SELECT name,ext,size,mtime,path FROM files WHERE name LIKE ?'
            params.append(f'%{query}%')

        alias = 'f.' if len(query) >= 3 else ''
        if ext and ext != "Tous":
            sql += f' AND {alias}ext=?'; params.append(ext.lower())
        if min_size is not None:
            sql += f' AND {alias}size>=?'; params.append(min_size)
        if max_size is not None:
            sql += f' AND {alias}size<=?'; params.append(max_size)
        if date_from is not None:
            sql += f' AND {alias}mtime>=?'; params.append(date_from)
        if date_to is not None:
            sql += f' AND {alias}mtime<=?'; params.append(date_to)
        sql += f' LIMIT {limit}'
        try:
            return c.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            sql2 = 'SELECT name,ext,size,mtime,path FROM files WHERE name LIKE ?'
            p2   = [f'%{query}%']
            if ext and ext != "Tous": sql2 += ' AND ext=?'; p2.append(ext.lower())
            sql2 += f' LIMIT {limit}'
            return c.execute(sql2, p2).fetchall()

    # ── Statistiques disque ──
    def disk_stats(self, top_n=15):
        c = self._conn()
        by_ext  = c.execute(
            "SELECT ext, COUNT(*) as n, SUM(size) as s FROM files "
            "GROUP BY ext ORDER BY s DESC LIMIT ?", (top_n,)).fetchall()
        by_dir  = c.execute(
            "SELECT dir, COUNT(*) as n, SUM(size) as s FROM files "
            "GROUP BY dir ORDER BY s DESC LIMIT ?", (top_n,)).fetchall()
        big     = c.execute(
            "SELECT name,ext,size,mtime,path FROM files "
            "ORDER BY size DESC LIMIT ?", (top_n,)).fetchall()
        return {"by_ext": by_ext, "by_dir": by_dir, "big_files": big}

    # ── Historique ──
    def add_history(self, query, results):
        c = self._conn()
        c.execute("INSERT INTO history(query,ts,results) VALUES(?,?,?)",
                  (query, time.time(), results))
        c.execute("DELETE FROM history WHERE id NOT IN "
                  "(SELECT id FROM history ORDER BY ts DESC LIMIT 100)")
        c.commit()

    def get_history(self, limit=30):
        c = self._conn()
        return c.execute(
            "SELECT query,ts,results FROM history ORDER BY ts DESC LIMIT ?",
            (limit,)).fetchall()

    def clear_history(self):
        self._conn().execute("DELETE FROM history"); self._conn().commit()

    # ── Favoris ──
    def add_favorite(self, path, name):
        c = self._conn()
        c.execute("INSERT OR IGNORE INTO favorites VALUES(?,?,?)",
                  (path, name, time.time())); c.commit()

    def remove_favorite(self, path):
        c = self._conn()
        c.execute("DELETE FROM favorites WHERE path=?", (path,)); c.commit()

    def get_favorites(self):
        return self._conn().execute(
            "SELECT path,name FROM favorites ORDER BY added DESC").fetchall()

    def is_favorite(self, path):
        return self._conn().execute(
            "SELECT 1 FROM favorites WHERE path=?", (path,)).fetchone() is not None


# ═══════════════════════════════════════════════════════════════════════════════
#  FENÊTRE PRINCIPALE
# ═══════════════════════════════════════════════════════════════════════════════
class FileFinder(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FileFinder Pro")
        self.geometry("1300x800")
        self.minsize(1000, 600)

        self.db         = IndexDB()
        self._stop      = threading.Event()
        self._sort_asc  = {}
        self._conf      = self._load_conf()
        self._cur_theme = self._conf.get("theme", "Sombre")
        self._apply_theme(self._cur_theme)

        self.configure(bg=T["BG"])
        self._build_styles()
        self._build_ui()
        self._refresh_sb()
        updater.check_and_notify(self)   # ← vérifie les mises à jour au démarrage

    # ── Config ────────────────────────────────────────────────────────────────
    def _load_conf(self):
        try:
            with open(CONF_PATH) as f: return json.load(f)
        except: return {}

    def _save_conf(self):
        self._conf["theme"] = self._cur_theme
        with open(CONF_PATH, "w") as f: json.dump(self._conf, f)

    def _apply_theme(self, name):
        global T
        T = THEMES.get(name, THEMES["Sombre"]).copy()
        self._cur_theme = name

    # ── Styles ttk ────────────────────────────────────────────────────────────
    def _build_styles(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("Treeview",
                    background=T["ROW_A"], foreground=T["TEXT"],
                    fieldbackground=T["ROW_A"],
                    rowheight=27, font=("Segoe UI", 9), borderwidth=0)
        s.configure("Treeview.Heading",
                    background=T["PANEL"], foreground=T["ACCENT_LT"],
                    font=("Segoe UI", 9, "bold"), relief="flat")
        s.map("Treeview",
              background=[("selected", T["SEL"])],
              foreground=[("selected", T["TEXT"])])
        s.configure("TProgressbar",
                    troughcolor=T["PANEL"], background=T["ACCENT"],
                    borderwidth=0)
        s.configure("TCombobox",
                    fieldbackground=T["ENTRY_BG"], background=T["PANEL"],
                    foreground=T["TEXT"], selectbackground=T["ACCENT"])
        s.map("TCombobox",
              fieldbackground=[("readonly", T["ENTRY_BG"])],
              foreground=[("readonly", T["TEXT"])])
        s.configure("TNotebook",
                    background=T["BG"], borderwidth=0)
        s.configure("TNotebook.Tab",
                    background=T["CARD"], foreground=T["TEXT_DIM"],
                    padding=[12, 5], font=("Segoe UI", 9))
        s.map("TNotebook.Tab",
              background=[("selected", T["PANEL"])],
              foreground=[("selected", T["ACCENT_LT"])])

    # ── UI principale ─────────────────────────────────────────────────────────
    def _build_ui(self):
        # Topbar
        top = tk.Frame(self, bg=T["PANEL"], height=52)
        top.pack(fill="x"); top.pack_propagate(False)
        tk.Label(top, text="⚡ FileFinder Pro",
                 font=("Segoe UI", 15, "bold"),
                 bg=T["PANEL"], fg=T["ACCENT_LT"]).pack(side="left", padx=18)

        self._ibtn(top, "🔨 Créer index",     self._do_rebuild).pack(side="right", padx=3, pady=10)
        self._ibtn(top, "🔄 Mettre à jour",   self._do_update ).pack(side="right", padx=3, pady=10)
        self._ibtn(top, "⚙ Dossiers",         self._manage_roots).pack(side="right", padx=3, pady=10)
        self._ibtn(top, "🌓 Thème",            self._toggle_theme).pack(side="right", padx=3, pady=10)

        tk.Frame(self, bg=T["BORDER"], height=1).pack(fill="x")

        # Notebook (onglets)
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=0, pady=0)

        self._tab_search  = tk.Frame(self.nb, bg=T["BG"])
        self._tab_favs    = tk.Frame(self.nb, bg=T["BG"])
        self._tab_history = tk.Frame(self.nb, bg=T["BG"])
        self._tab_stats   = tk.Frame(self.nb, bg=T["BG"])

        self.nb.add(self._tab_search,  text="🔍 Recherche")
        self.nb.add(self._tab_favs,    text="⭐ Favoris")
        self.nb.add(self._tab_history, text="🕐 Historique")
        self.nb.add(self._tab_stats,   text="📊 Statistiques disque")

        self._build_search_tab()
        self._build_favs_tab()
        self._build_history_tab()
        self._build_stats_tab()

        # Barre de statut globale
        sb = tk.Frame(self, bg=T["PANEL"], height=26)
        sb.pack(fill="x", side="bottom"); sb.pack_propagate(False)
        self.var_sb = tk.StringVar()
        tk.Label(sb, textvariable=self.var_sb,
                 font=("Segoe UI", 8), bg=T["PANEL"], fg=T["TEXT_DIM"]
                 ).pack(side="left", padx=10)

        # Menu contextuel
        self.ctx = tk.Menu(self, tearoff=0, bg=T["CARD"], fg=T["TEXT"],
                           activebackground=T["ACCENT"],
                           activeforeground="white",
                           font=("Segoe UI", 9))
        self.ctx.add_command(label="📂 Ouvrir le dossier",  command=self._open_folder)
        self.ctx.add_command(label="▶  Ouvrir le fichier",  command=self._open_file)
        self.ctx.add_separator()
        self.ctx.add_command(label="⭐ Ajouter aux favoris", command=self._toggle_fav)
        self.ctx.add_command(label="📋 Copier le chemin",    command=self._copy_path)
        self.ctx.add_command(label="📋 Copier le nom",       command=self._copy_name)

    # ─────────────────────────────────────────────────────────────────────────
    #  ONGLET RECHERCHE
    # ─────────────────────────────────────────────────────────────────────────
    def _build_search_tab(self):
        p = self._tab_search

        # ── Zone de saisie ──
        sf = tk.Frame(p, bg=T["BG"])
        sf.pack(fill="x", padx=20, pady=14)
        sf.grid_columnconfigure(0, weight=1)

        wrap = tk.Frame(sf, bg=T["ACCENT"], padx=1, pady=1)
        wrap.grid(row=0, column=0, sticky="ew")
        wrap.grid_columnconfigure(0, weight=1)
        self.var_q = tk.StringVar()
        self.var_q.trace_add("write", self._on_type)
        self.entry = tk.Entry(wrap, textvariable=self.var_q,
                              font=("Segoe UI", 13),
                              bg=T["ENTRY_BG"], fg=T["TEXT"],
                              insertbackground=T["ACCENT_LT"],
                              relief="flat", bd=0)
        self.entry.grid(row=0, column=0, sticky="ew", ipady=9, padx=10)
        self.entry.bind("<Return>", lambda _: self._launch_search())

        self._sbtn(sf, "🔍 Rechercher", self._launch_search, T["ACCENT"]
                   ).grid(row=0, column=1, padx=(8,0))
        self._sbtn(sf, "✕",             self._clear_results, T["CARD"]
                   ).grid(row=0, column=2, padx=(4,0))

        # ── Filtres de base ──
        ff = tk.Frame(p, bg=T["BG"])
        ff.pack(fill="x", padx=20, pady=(0, 6))

        tk.Label(ff, text="Type :", font=("Segoe UI", 9),
                 bg=T["BG"], fg=T["TEXT_DIM"]).pack(side="left")
        self.var_ext = tk.StringVar(value="Tous")
        ttk.Combobox(ff, textvariable=self.var_ext, width=10,
                     values=["Tous",".pdf",".docx",".xlsx",".txt",".csv",
                             ".py",".jpg",".png",".mp4",".mp3",".zip",
                             ".exe",".pptx",".html"],
                     state="readonly", font=("Segoe UI", 9)
                     ).pack(side="left", padx=(5,18))

        self.var_case    = tk.BooleanVar()
        self.var_content = tk.BooleanVar()
        self._ckbx(ff, "Casse",          self.var_case   ).pack(side="left", padx=4)
        self._ckbx(ff, "Dans le contenu",self.var_content).pack(side="left", padx=4)

        # ── Filtres avancés (taille & date) ──
        af = tk.Frame(p, bg=T["BG"])
        af.pack(fill="x", padx=20, pady=(0, 8))

        tk.Label(af, text="Taille min (Mo):", font=("Segoe UI", 9),
                 bg=T["BG"], fg=T["TEXT_DIM"]).pack(side="left")
        self.var_smin = tk.StringVar()
        tk.Entry(af, textvariable=self.var_smin, width=7,
                 bg=T["ENTRY_BG"], fg=T["TEXT"], insertbackground=T["TEXT"],
                 relief="flat", font=("Segoe UI", 9),
                 highlightthickness=1, highlightbackground=T["BORDER"]
                 ).pack(side="left", padx=(4,12), ipady=3)

        tk.Label(af, text="Taille max (Mo):", font=("Segoe UI", 9),
                 bg=T["BG"], fg=T["TEXT_DIM"]).pack(side="left")
        self.var_smax = tk.StringVar()
        tk.Entry(af, textvariable=self.var_smax, width=7,
                 bg=T["ENTRY_BG"], fg=T["TEXT"], insertbackground=T["TEXT"],
                 relief="flat", font=("Segoe UI", 9),
                 highlightthickness=1, highlightbackground=T["BORDER"]
                 ).pack(side="left", padx=(4,18), ipady=3)

        tk.Label(af, text="Depuis (AAAA-MM-JJ):", font=("Segoe UI", 9),
                 bg=T["BG"], fg=T["TEXT_DIM"]).pack(side="left")
        self.var_dfrom = tk.StringVar()
        tk.Entry(af, textvariable=self.var_dfrom, width=12,
                 bg=T["ENTRY_BG"], fg=T["TEXT"], insertbackground=T["TEXT"],
                 relief="flat", font=("Segoe UI", 9),
                 highlightthickness=1, highlightbackground=T["BORDER"]
                 ).pack(side="left", padx=(4,12), ipady=3)

        tk.Label(af, text="Jusqu'au:", font=("Segoe UI", 9),
                 bg=T["BG"], fg=T["TEXT_DIM"]).pack(side="left")
        self.var_dto = tk.StringVar()
        tk.Entry(af, textvariable=self.var_dto, width=12,
                 bg=T["ENTRY_BG"], fg=T["TEXT"], insertbackground=T["TEXT"],
                 relief="flat", font=("Segoe UI", 9),
                 highlightthickness=1, highlightbackground=T["BORDER"]
                 ).pack(side="left", padx=(4,0), ipady=3)

        # ── Progression ──
        pf = tk.Frame(p, bg=T["BG"])
        pf.pack(fill="x", padx=20)
        self.var_status = tk.StringVar(value="Prêt — tapez un mot-clé.")
        tk.Label(pf, textvariable=self.var_status,
                 font=("Segoe UI", 9), bg=T["BG"], fg=T["TEXT_DIM"]
                 ).pack(side="left")
        self.var_count = tk.StringVar()
        tk.Label(pf, textvariable=self.var_count,
                 font=("Segoe UI", 9, "bold"), bg=T["BG"], fg=T["GREEN"]
                 ).pack(side="right")
        self.progress = ttk.Progressbar(p, mode="indeterminate")
        self.progress.pack(fill="x", padx=20, pady=(3,0))

        # ── Panneau résultats + aperçu ──
        pane = tk.PanedWindow(p, orient="horizontal",
                              bg=T["BORDER"], sashwidth=4,
                              sashrelief="flat")
        pane.pack(fill="both", expand=True, padx=20, pady=8)

        # Tableau
        tree_wrap = tk.Frame(pane, bg=T["BORDER"], padx=1, pady=1)
        tree_inner = tk.Frame(tree_wrap, bg=T["BG"])
        tree_inner.pack(fill="both", expand=True)

        cols = ("★","Nom","Type","Taille","Modifié","Chemin complet")
        self.tree = ttk.Treeview(tree_inner, columns=cols,
                                 show="headings", selectmode="browse")
        ws = [28, 220, 60, 85, 140, 500]
        for col, w in zip(cols, ws):
            self.tree.heading(col, text=col,
                              command=lambda c=col: self._sort(c))
            self.tree.column(col, width=w, anchor="w", minwidth=24)
        vsb = ttk.Scrollbar(tree_inner, orient="vertical",   command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_inner, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_inner.grid_rowconfigure(0, weight=1)
        tree_inner.grid_columnconfigure(0, weight=1)
        self.tree.tag_configure("odd",  background=T["ROW_A"])
        self.tree.tag_configure("even", background=T["ROW_B"])
        self.tree.tag_configure("fav",  background=T["ACCENT_DIM"])
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>",          self._open_folder)
        self.tree.bind("<Button-3>",          self._ctx_popup)
        pane.add(tree_wrap, stretch="always")

        # Aperçu
        prev_wrap = tk.Frame(pane, bg=T["CARD"], padx=1, pady=1)
        prev_inner = tk.Frame(prev_wrap, bg=T["CARD"])
        prev_inner.pack(fill="both", expand=True)
        tk.Label(prev_inner, text="Aperçu",
                 font=("Segoe UI", 9, "bold"),
                 bg=T["CARD"], fg=T["ACCENT_LT"]).pack(anchor="w", padx=8, pady=4)
        self.prev_text = tk.Text(prev_inner,
                                 font=("Consolas", 9),
                                 bg=T["ENTRY_BG"], fg=T["TEXT"],
                                 wrap="none", relief="flat",
                                 state="disabled",
                                 insertbackground=T["TEXT"])
        sv = ttk.Scrollbar(prev_inner, command=self.prev_text.yview)
        self.prev_text.configure(yscrollcommand=sv.set)
        self.prev_text.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        sv.pack(side="right", fill="y", pady=4)
        pane.add(prev_wrap, width=340)

        # ── Actions bas ──
        bot = tk.Frame(p, bg=T["BG"])
        bot.pack(fill="x", padx=20, pady=(0,12))
        self._sbtn(bot, "📂 Ouvrir dossier",  self._open_folder,   T["ACCENT"]).pack(side="left", padx=(0,5))
        self._sbtn(bot, "▶ Ouvrir fichier",   self._open_file,     T["CARD"]  ).pack(side="left", padx=(0,5))
        self._sbtn(bot, "⭐ Favori",           self._toggle_fav,    T["CARD"]  ).pack(side="left", padx=(0,5))
        self._sbtn(bot, "📋 Copier chemin",    self._copy_path,     T["CARD"]  ).pack(side="left", padx=(0,5))
        self._sbtn(bot, "💾 Exporter",         self._export,        T["CARD"]  ).pack(side="left")
        self._sbtn(bot, "⏹ Arrêter",          self._stop_search,   T["RED"]   ).pack(side="right")

    # ─────────────────────────────────────────────────────────────────────────
    #  ONGLET FAVORIS
    # ─────────────────────────────────────────────────────────────────────────
    def _build_favs_tab(self):
        p = self._tab_favs
        tk.Label(p, text="⭐ Fichiers favoris",
                 font=("Segoe UI", 12, "bold"),
                 bg=T["BG"], fg=T["ACCENT_LT"]).pack(anchor="w", padx=20, pady=(14,6))

        wrap = tk.Frame(p, bg=T["BORDER"], padx=1, pady=1)
        wrap.pack(fill="both", expand=True, padx=20, pady=(0,8))
        inner = tk.Frame(wrap, bg=T["BG"])
        inner.pack(fill="both", expand=True)

        cols = ("Nom","Chemin","Ajouté le")
        self.fav_tree = ttk.Treeview(inner, columns=cols,
                                     show="headings", selectmode="browse")
        for col, w in zip(cols, [220, 600, 140]):
            self.fav_tree.heading(col, text=col)
            self.fav_tree.column(col, width=w, anchor="w")
        vsb = ttk.Scrollbar(inner, orient="vertical", command=self.fav_tree.yview)
        self.fav_tree.configure(yscrollcommand=vsb.set)
        self.fav_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.fav_tree.bind("<Double-1>", lambda _: self._fav_open())

        bot = tk.Frame(p, bg=T["BG"])
        bot.pack(fill="x", padx=20, pady=(0,14))
        self._sbtn(bot, "📂 Ouvrir dossier", self._fav_open,   T["ACCENT"]).pack(side="left", padx=(0,6))
        self._sbtn(bot, "🗑 Retirer",         self._fav_remove, T["RED"]   ).pack(side="left")

        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_change)

    def _load_favs(self):
        for i in self.fav_tree.get_children():
            self.fav_tree.delete(i)
        for path, name in self.db.get_favorites():
            try:
                st = os.stat(path)
                added = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d")
            except:
                added = "—"
            self.fav_tree.insert("", "end", values=(name or os.path.basename(path), path, added))

    def _fav_open(self):
        sel = self.fav_tree.selection()
        if sel:
            path = self.fav_tree.item(sel[0], "values")[1]
            if os.path.exists(path):
                subprocess.Popen(f'explorer /select,"{path}"')

    def _fav_remove(self):
        sel = self.fav_tree.selection()
        if sel:
            path = self.fav_tree.item(sel[0], "values")[1]
            self.db.remove_favorite(path)
            self._load_favs()

    # ─────────────────────────────────────────────────────────────────────────
    #  ONGLET HISTORIQUE
    # ─────────────────────────────────────────────────────────────────────────
    def _build_history_tab(self):
        p = self._tab_history
        tk.Label(p, text="🕐 Historique des recherches",
                 font=("Segoe UI", 12, "bold"),
                 bg=T["BG"], fg=T["ACCENT_LT"]).pack(anchor="w", padx=20, pady=(14,6))

        wrap = tk.Frame(p, bg=T["BORDER"], padx=1, pady=1)
        wrap.pack(fill="both", expand=True, padx=20, pady=(0,8))
        inner = tk.Frame(wrap, bg=T["BG"])
        inner.pack(fill="both", expand=True)

        cols = ("Recherche","Date","Résultats")
        self.hist_tree = ttk.Treeview(inner, columns=cols,
                                      show="headings", selectmode="browse")
        for col, w in zip(cols, [300, 180, 100]):
            self.hist_tree.heading(col, text=col)
            self.hist_tree.column(col, width=w, anchor="w")
        vsb = ttk.Scrollbar(inner, orient="vertical", command=self.hist_tree.yview)
        self.hist_tree.configure(yscrollcommand=vsb.set)
        self.hist_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.hist_tree.bind("<Double-1>", self._hist_reuse)

        bot = tk.Frame(p, bg=T["BG"])
        bot.pack(fill="x", padx=20, pady=(0,14))
        self._sbtn(bot, "🔍 Relancer cette recherche", self._hist_reuse,    T["ACCENT"]).pack(side="left", padx=(0,6))
        self._sbtn(bot, "🗑 Effacer l'historique",      self._hist_clear,   T["RED"]   ).pack(side="left")

    def _load_history(self):
        for i in self.hist_tree.get_children():
            self.hist_tree.delete(i)
        for query, ts, results in self.db.get_history():
            dt = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            self.hist_tree.insert("", "end", values=(query, dt, results))

    def _hist_reuse(self, _=None):
        sel = self.hist_tree.selection()
        if sel:
            q = self.hist_tree.item(sel[0], "values")[0]
            self.var_q.set(q)
            self.nb.select(0)
            self._launch_search()

    def _hist_clear(self):
        if messagebox.askyesno("Effacer", "Effacer tout l'historique ?"):
            self.db.clear_history()
            self._load_history()

    # ─────────────────────────────────────────────────────────────────────────
    #  ONGLET STATISTIQUES DISQUE
    # ─────────────────────────────────────────────────────────────────────────
    def _build_stats_tab(self):
        p = self._tab_stats

        hdr = tk.Frame(p, bg=T["BG"])
        hdr.pack(fill="x", padx=20, pady=(14,8))
        tk.Label(hdr, text="📊 Statistiques du disque indexé",
                 font=("Segoe UI", 12, "bold"),
                 bg=T["BG"], fg=T["ACCENT_LT"]).pack(side="left")
        self._sbtn(hdr, "🔄 Actualiser", self._load_stats, T["ACCENT"]
                   ).pack(side="right")

        # Résumé global
        self.stats_summary = tk.Label(p, text="",
                                      font=("Segoe UI", 10),
                                      bg=T["BG"], fg=T["TEXT"],
                                      justify="left")
        self.stats_summary.pack(anchor="w", padx=20, pady=(0,10))

        pane2 = tk.PanedWindow(p, orient="horizontal",
                               bg=T["BORDER"], sashwidth=4)
        pane2.pack(fill="both", expand=True, padx=20, pady=(0,8))

        # Par extension
        f1 = tk.Frame(pane2, bg=T["BG"])
        tk.Label(f1, text="Par type de fichier",
                 font=("Segoe UI", 9, "bold"),
                 bg=T["BG"], fg=T["TEXT_DIM"]).pack(anchor="w", padx=4, pady=4)
        self.ext_tree = self._mini_tree(f1,
                        ("Extension","Fichiers","Taille totale"))
        pane2.add(f1, stretch="always")

        # Par dossier
        f2 = tk.Frame(pane2, bg=T["BG"])
        tk.Label(f2, text="Dossiers les plus lourds",
                 font=("Segoe UI", 9, "bold"),
                 bg=T["BG"], fg=T["TEXT_DIM"]).pack(anchor="w", padx=4, pady=4)
        self.dir_tree = self._mini_tree(f2,
                        ("Dossier","Fichiers","Taille totale"))
        pane2.add(f2, stretch="always")

        # Fichiers les plus volumineux
        f3 = tk.Frame(p, bg=T["BG"])
        f3.pack(fill="x", padx=20, pady=(0,14))
        tk.Label(f3, text="📦 Fichiers les plus volumineux",
                 font=("Segoe UI", 9, "bold"),
                 bg=T["BG"], fg=T["TEXT_DIM"]).pack(anchor="w", pady=4)
        self.big_tree = self._mini_tree(f3,
                        ("Nom","Taille","Chemin"), heights=8)
        self.big_tree.bind("<Double-1>",
                           lambda _: self._stats_open(self.big_tree, 2))

    def _mini_tree(self, parent, cols, heights=10):
        wrap = tk.Frame(parent, bg=T["BORDER"], padx=1, pady=1)
        wrap.pack(fill="both", expand=True)
        inner = tk.Frame(wrap, bg=T["BG"])
        inner.pack(fill="both", expand=True)
        t = ttk.Treeview(inner, columns=cols, show="headings",
                         height=heights, selectmode="browse")
        for col in cols:
            t.heading(col, text=col)
            t.column(col, width=160, anchor="w")
        vsb = ttk.Scrollbar(inner, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=vsb.set)
        t.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        return t

    def _load_stats(self):
        st  = self.db.stats()
        ds  = self.db.disk_stats()
        self.stats_summary.config(
            text=f"Fichiers indexés : {st['count']:,}   •   "
                 f"Espace total : {self._hsize(st['total_size'])}   •   "
                 f"Dernière indexation : {st['last_index'] or '—'}")
        for t_widget, data in [(self.ext_tree, ds["by_ext"]),
                               (self.dir_tree, ds["by_dir"])]:
            for i in t_widget.get_children(): t_widget.delete(i)
            for row in data:
                label = row[0] or "(sans ext.)"
                if len(label) > 55: label = "…" + label[-54:]
                t_widget.insert("", "end",
                                values=(label, f"{row[1]:,}", self._hsize(row[2])))
        for i in self.big_tree.get_children(): self.big_tree.delete(i)
        for row in ds["big_files"]:
            self.big_tree.insert("", "end",
                                 values=(row[0], self._hsize(row[2]), row[4]))

    def _stats_open(self, tree, path_col):
        sel = tree.selection()
        if sel:
            path = tree.item(sel[0], "values")[path_col]
            if os.path.exists(path):
                subprocess.Popen(f'explorer /select,"{path}"')

    # ─────────────────────────────────────────────────────────────────────────
    #  NAVIGATION ONGLETS
    # ─────────────────────────────────────────────────────────────────────────
    def _on_tab_change(self, _=None):
        idx = self.nb.index("current")
        if idx == 1: self._load_favs()
        if idx == 2: self._load_history()
        if idx == 3: self._load_stats()

    # ─────────────────────────────────────────────────────────────────────────
    #  INDEXATION
    # ─────────────────────────────────────────────────────────────────────────
    def _do_rebuild(self):
        roots = self.db.stats()["roots"] or ["C:\\"]
        if not messagebox.askyesno("Créer l'index",
                "Indexer :\n" + "\n".join(roots) +
                "\n\nCela peut prendre quelques minutes."):
            return
        self._run_index(self.db.rebuild, roots, "Indexation complète")

    def _do_update(self):
        roots = self.db.stats()["roots"] or ["C:\\"]
        self._run_index(self.db.update, roots, "Mise à jour")

    def _run_index(self, fn, roots, label):
        self._stop.clear()
        self.progress.start(10)
        self.var_status.set(f"{label} en cours…")
        def run():
            t0 = time.time()
            res = fn(roots,
                     progress_cb=lambda n,p: self.after(
                         0, self.var_status.set,
                         f"{label} : {n:,} fichiers • {p[:60]}…"),
                     stop_flag=self._stop)
            e = time.time() - t0
            msg = (f"Index créé : {res:,} fichiers en {e:.1f}s"
                   if not isinstance(res, tuple)
                   else f"Mis à jour : +{res[0]:,} / -{res[1]:,} en {e:.1f}s")
            self.after(0, self._index_done, msg)
        threading.Thread(target=run, daemon=True).start()

    def _index_done(self, msg):
        self.progress.stop()
        self.var_status.set(msg)
        self._refresh_sb()

    # ─────────────────────────────────────────────────────────────────────────
    #  RECHERCHE
    # ─────────────────────────────────────────────────────────────────────────
    def _on_type(self, *_):
        if len(self.var_q.get().strip()) >= 3:
            self._launch_search()

    def _parse_filters(self):
        mn = mx = df = dt = None
        try:
            v = self.var_smin.get().strip()
            if v: mn = float(v) * 1024 * 1024
        except: pass
        try:
            v = self.var_smax.get().strip()
            if v: mx = float(v) * 1024 * 1024
        except: pass
        try:
            v = self.var_dfrom.get().strip()
            if v: df = datetime.strptime(v, "%Y-%m-%d").timestamp()
        except: pass
        try:
            v = self.var_dto.get().strip()
            if v: dt = datetime.strptime(v, "%Y-%m-%d").timestamp() + 86400
        except: pass
        return mn, mx, df, dt

    def _launch_search(self):
        q = self.var_q.get().strip()
        if not q: return
        self._clear_results()
        self._stop.clear()
        self.progress.start(10)
        mn, mx, df, dt = self._parse_filters()
        threading.Thread(
            target=self._search_thread,
            args=(q, self.var_ext.get(), mn, mx, df, dt),
            daemon=True).start()

    def _search_thread(self, q, ext, mn, mx, df, dt):
        t0   = time.time()
        rows = self.db.search(q, ext=ext, min_size=mn, max_size=mx,
                              date_from=df, date_to=dt)
        elapsed = time.time() - t0
        self.after(0, self._show_results, rows, elapsed, q)

    def _show_results(self, rows, elapsed, q):
        self.progress.stop()
        for i, (name, ext, size, mtime, path) in enumerate(rows):
            dt = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M") \
                 if mtime else "—"
            fav = "⭐" if self.db.is_favorite(path) else ""
            tag = "fav" if fav else ("odd" if i%2 else "even")
            self.tree.insert("", "end",
                             values=(fav, name, ext or "—",
                                     self._hsize(size), dt, path),
                             tags=(tag,))
        n = len(rows)
        self.var_count.set(f"{n:,} résultat(s)")
        self.var_status.set(
            f"Terminé en {elapsed*1000:.1f} ms — {n:,} résultat(s)")
        self.db.add_history(q, n)

    def _stop_search(self):
        self._stop.set(); self.progress.stop()
        self.var_status.set("Recherche interrompue.")

    # ─────────────────────────────────────────────────────────────────────────
    #  APERÇU
    # ─────────────────────────────────────────────────────────────────────────
    def _on_select(self, _=None):
        path = self._sel_path()
        if not path: return
        threading.Thread(target=self._load_preview, args=(path,), daemon=True).start()

    def _load_preview(self, path):
        ext = os.path.splitext(path)[1].lower()
        self.prev_text.configure(state="normal")
        self.prev_text.delete("1.0", "end")
        if ext in PREVIEW_EXTS:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(8000)
                self.after(0, self._set_preview, content)
            except Exception as e:
                self.after(0, self._set_preview, f"[Impossible de lire : {e}]")
        elif ext in IMG_EXTS:
            self.after(0, self._set_preview, f"[Image : {os.path.basename(path)}]\n\nDouble-cliquez pour ouvrir.")
        else:
            try:
                size = os.path.getsize(path)
                mtime = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
                info = (f"Fichier : {os.path.basename(path)}\n"
                        f"Type    : {ext or 'inconnu'}\n"
                        f"Taille  : {self._hsize(size)}\n"
                        f"Modifié : {mtime}\n"
                        f"Chemin  : {path}\n\n"
                        f"[Aperçu non disponible pour ce type de fichier]")
                self.after(0, self._set_preview, info)
            except:
                self.after(0, self._set_preview, "[Fichier inaccessible]")

    def _set_preview(self, text):
        self.prev_text.configure(state="normal")
        self.prev_text.delete("1.0", "end")
        self.prev_text.insert("1.0", text)
        self.prev_text.configure(state="disabled")

    # ─────────────────────────────────────────────────────────────────────────
    #  ACTIONS
    # ─────────────────────────────────────────────────────────────────────────
    def _sel_path(self):
        s = self.tree.selection()
        return self.tree.item(s[0], "values")[5] if s else None

    def _open_folder(self, _=None):
        p = self._sel_path()
        if p: subprocess.Popen(f'explorer /select,"{p}"')

    def _open_file(self, _=None):
        p = self._sel_path()
        if p and os.path.exists(p): os.startfile(p)

    def _toggle_fav(self):
        p = self._sel_path()
        if not p: return
        if self.db.is_favorite(p):
            self.db.remove_favorite(p)
        else:
            self.db.add_favorite(p, os.path.basename(p))
        self._launch_search()

    def _copy_path(self):
        p = self._sel_path()
        if p:
            self.clipboard_clear(); self.clipboard_append(p)
            self.var_status.set(f"Copié : {p}")

    def _copy_name(self):
        s = self.tree.selection()
        if s:
            n = self.tree.item(s[0], "values")[1]
            self.clipboard_clear(); self.clipboard_append(n)

    def _export(self):
        if not self.tree.get_children():
            messagebox.showinfo("Info", "Aucun résultat à exporter."); return
        fp = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Texte", "*.txt"), ("CSV", "*.csv")])
        if not fp: return
        with open(fp, "w", encoding="utf-8") as f:
            f.write(f"FileFinder Pro v3 — {datetime.now():%Y-%m-%d %H:%M}\n")
            f.write("─"*80 + "\n")
            for item in self.tree.get_children():
                f.write(self.tree.item(item,"values")[5] + "\n")
        messagebox.showinfo("Exporté", f"Enregistré :\n{fp}")

    def _clear_results(self):
        for i in self.tree.get_children(): self.tree.delete(i)
        self.var_count.set("")
        self._set_preview("")

    # ─────────────────────────────────────────────────────────────────────────
    #  THÈME
    # ─────────────────────────────────────────────────────────────────────────
    def _toggle_theme(self):
        self._cur_theme = "Clair" if self._cur_theme == "Sombre" else "Sombre"
        self._apply_theme(self._cur_theme)
        self._save_conf()
        messagebox.showinfo("Thème",
            f"Thème «{self._cur_theme}» activé.\nRedémarrez le logiciel pour appliquer.")

    # ─────────────────────────────────────────────────────────────────────────
    #  GESTION DOSSIERS INDEXÉS
    # ─────────────────────────────────────────────────────────────────────────
    def _manage_roots(self):
        win = tk.Toplevel(self)
        win.title("Dossiers indexés")
        win.configure(bg=T["BG"])
        win.geometry("560x340")
        win.grab_set()
        tk.Label(win, text="Dossiers à indexer",
                 font=("Segoe UI", 12, "bold"),
                 bg=T["BG"], fg=T["ACCENT_LT"]).pack(pady=(16,8))
        lb = tk.Listbox(win, bg=T["ENTRY_BG"], fg=T["TEXT"],
                        selectbackground=T["ACCENT"],
                        font=("Segoe UI", 10), relief="flat",
                        highlightthickness=1,
                        highlightbackground=T["BORDER"])
        lb.pack(fill="both", expand=True, padx=20)
        for r in (self.db.stats()["roots"] or ["C:\\"]):
            lb.insert("end", r)
        br = tk.Frame(win, bg=T["BG"]); br.pack(pady=12)
        def add():
            d = filedialog.askdirectory()
            if d: lb.insert("end", d.replace("/","\\"))
        def rem():
            sel = lb.curselection()
            if sel: lb.delete(sel[0])
        def save():
            c = self.db._conn()
            c.execute("REPLACE INTO meta VALUES('roots',?)",
                      (json.dumps(list(lb.get(0,"end"))),))
            c.commit(); win.destroy()
        self._sbtn(br,"➕ Ajouter",add,T["ACCENT"]).pack(side="left",padx=6)
        self._sbtn(br,"🗑 Supprimer",rem,T["CARD"] ).pack(side="left",padx=6)
        self._sbtn(br,"✅ Enregistrer",save,T["GREEN"]).pack(side="left",padx=6)

    # ─────────────────────────────────────────────────────────────────────────
    #  TRI
    # ─────────────────────────────────────────────────────────────────────────
    def _sort(self, col):
        asc = not self._sort_asc.get(col, False)
        self._sort_asc[col] = asc
        data = [(self.tree.set(k, col), k) for k in self.tree.get_children()]
        data.sort(reverse=not asc)
        for idx, (_,k) in enumerate(data):
            self.tree.move(k,"",idx)
            tag = "fav" if self.tree.item(k,"values")[0]=="⭐" \
                  else ("odd" if idx%2 else "even")
            self.tree.item(k, tags=(tag,))

    # ─────────────────────────────────────────────────────────────────────────
    #  UTILITAIRES
    # ─────────────────────────────────────────────────────────────────────────
    def _refresh_sb(self):
        st = self.db.stats()
        if st["count"]:
            self.var_sb.set(
                f"Index : {st['count']:,} fichiers • "
                f"{self._hsize(st['total_size'])} • "
                f"Dernière indexation : {st['last_index'] or '—'}")
        else:
            self.var_sb.set("Aucun index — cliquez sur 🔨 Créer index.")

    def _ctx_popup(self, e):
        item = self.tree.identify_row(e.y)
        if item:
            self.tree.selection_set(item)
            self.ctx.tk_popup(e.x_root, e.y_root)

    def _ibtn(self, parent, text, cmd):
        return tk.Button(parent, text=text, command=cmd,
                         bg=T["CARD"], fg=T["ACCENT_LT"],
                         font=("Segoe UI", 8, "bold"),
                         relief="flat", cursor="hand2",
                         padx=10, pady=0, bd=0,
                         activebackground=T["ACCENT_DIM"],
                         activeforeground="white")

    def _sbtn(self, parent, text, cmd, color):
        return tk.Button(parent, text=text, command=cmd,
                         bg=color, fg="white",
                         font=("Segoe UI", 9, "bold"),
                         relief="flat", cursor="hand2",
                         padx=10, pady=5, bd=0,
                         activebackground=T["ACCENT_LT"],
                         activeforeground="white")

    def _ckbx(self, parent, text, var):
        return tk.Checkbutton(parent, text=text, variable=var,
                              bg=T["BG"], fg=T["TEXT_DIM"],
                              selectcolor=T["ENTRY_BG"],
                              activebackground=T["BG"],
                              font=("Segoe UI", 9))

    @staticmethod
    def _hsize(n):
        try: n = int(n)
        except: return "—"
        for u in ("o","Ko","Mo","Go"):
            if n < 1024: return f"{n:.0f} {u}"
            n /= 1024
        return f"{n:.1f} To"


# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    app = FileFinder()
    app.mainloop()
