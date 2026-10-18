import re
import shutil

F = "scalp_bot_local.py"
shutil.copy(F, F + ".bak")
src = open(F, encoding="utf-8").read()

if "class ScrollableFrame" in src:
    raise SystemExit("Já está aplicado.")

SCROLL = '''class ScrollableFrame(tk.Frame):
    def __init__(self, parent, bg):
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0)
        vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self._win, width=e.width))
        self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))
        self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))


'''

# 1) tamanho da janela
src = src.replace('self.geometry("860x1030")', 'self.geometry("860x700")', 1)

# 2) localizar o _build e o fim dele
i = src.index("    def _build(self):")
m = re.search(r"\n    def ", src[i + 10:])
j = i + 10 + m.start() if m else len(src)
body = src[i:j]

# 3) widgets que tinham "self" como parent passam a ter "host"
body = re.sub(r"\b((?:tk|ttk)\.\w+)\(self([,)])", r"\1(host\2", body)

# 4) criar o scroll logo a seguir à linha do pad
pad = 'pad = {"padx": 10, "pady": 6}\n'
body = body.replace(
    pad,
    pad + '        _sc = ScrollableFrame(self, self.BG)\n'
          '        _sc.pack(fill="both", expand=True)\n'
          '        host = _sc.inner\n',
    1,
)
src = src[:i] + body + src[j:]

# 5) inserir a classe antes da classe da janela principal
k = src.rfind("\nclass ", 0, src.index("    def _build(self):"))
src = src[:k + 1] + SCROLL + src[k + 1:]

open(F, "w", encoding="utf-8").write(src)
print("Feito. Cópia de segurança em", F + ".bak")
