"""Scalp Bot cbBTC/WETH (Base) - aplicação LOCAL (Tkinter).

- Carteira REAL: importar chave privada, ver saldos, enviar TODO o saldo.
- Trading: SIMULAÇÃO ou REAL (caixa "Modo REAL"), swaps no Uniswap V3 (Base).
- O GEMINI GERE A CARTEIRA como trader discricionário: decide quando comprar, quando vender
  e quanto arriscar. NÃO há take profit, stop loss, trailing nem limiares de RSI definidos.
- Só ficam limites de proteção da conta (não são regras de trading): slippage máx., perda total
  máx. (kill switch), cooldown opcional, confiança mínima e tamanho máximo por ordem.
- Extras: preço/saída calculados na própria pool (preço executável), botão "Fechar posição",
  registo em trades.csv e retoma da posição REAL após reiniciar (estado.json).
"""
import csv
import json
import os
import queue
import re
import threading
import time
import tkinter as tk
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from tkinter import messagebox, scrolledtext, ttk

import pandas as pd
import requests
from dotenv import load_dotenv
from eth_account import Account
from google import genai
from web3 import Web3

load_dotenv()

# ---------- Configuração fixa (.env) ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KEYSTORE = os.path.join(BASE_DIR, "wallet.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")
STATE_FILE = os.path.join(BASE_DIR, "estado.json")
TRADES_CSV = os.path.join(BASE_DIR, "trades.csv")
GEMINI_KEY_FILE = os.path.join(BASE_DIR, "gemini.key")
LOGO_FILE = os.path.join(BASE_DIR, "falcon.png")
AMOSTRA_RAPIDA_S = 2   # sem histórico: recolhe amostras a este ritmo até haver dados suficientes
# Se o Gemini ficar indisponível (todos os modelos esgotados) com posição aberta, fecha-a ao fim destes minutos
# em vez de ficar às cegas. 0 = nunca fecha por causa disto.
GEMINI_FALHA_FECHAR_MIN = float(os.getenv("GEMINI_FALHA_FECHAR_MIN", "30"))
MIN_AMOSTRAS = 40  # mínimo para MACD(12,26,9), RSI14, Bollinger20, ATR14, EMA50 parcial, etc.

RPC_URL = os.getenv("RPC_URL", "https://mainnet.base.org")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
FEE_PCT = float(os.getenv("FEE_PCT", "0.05")) / 100            # custo por swap na simulação
LIVE_TRADING = os.getenv("LIVE_TRADING", "0").strip().lower() in ("1", "true", "yes")
MIN_ETH_GAS = float(os.getenv("MIN_ETH_GAS", "0.0005"))        # ETH mínimo para pagar gas
POOL_FEE = int(os.getenv("POOL_FEE", "0"))                     # 0 = detetar a pool com mais liquidez
_CAPITAL = float(os.getenv("TRADE_WETH", "0.01")) if LIVE_TRADING else float(os.getenv("SIM_CAPITAL_WETH", "0.1"))

# ---------- Parâmetros editáveis na interface ----------
# (chave, rótulo, default, mínimo, máximo, tipo)
FIELDS = [
    ("capital", "Capital (WETH)", _CAPITAL, 0.0001, 1000.0, float),
    ("order_pct", "Máx. % do capital por ordem", float(os.getenv("ORDER_PCT", "100")), 1.0, 100.0, float),
    ("min_conf", "Confiança mín. Gemini", float(os.getenv("MIN_CONFIDENCE", "0.6")), 0.0, 1.0, float),
    ("slippage_pct", "Slippage máx. %", float(os.getenv("SLIPPAGE_PCT", "0.5")), 0.05, 5.0, float),
    ("max_loss_pct", "Perda total máx. %", float(os.getenv("MAX_TOTAL_LOSS_PCT", "5")), 0.5, 100.0, float),
    ("loop_s", "Intervalo do ciclo (s)", int(os.getenv("LOOP_SECONDS", "60")), 10, 3600, int),
    ("cooldown_s", "Cooldown após fecho (s)", int(os.getenv("COOLDOWN_SECONDS", "120")), 0, 86400, int),
    ("max_losses", "Perdas seguidas máx. (0=off)", int(os.getenv("MAX_CONSECUTIVE_LOSSES", "3")), 0, 20, int),
]


class Settings:
    """Parâmetros partilhados entre a janela e o bot. settings.json sobrepõe os defaults do .env."""

    def __init__(self):
        self.v = {k: d for k, _l, d, _lo, _hi, _t in FIELDS}
        self.load()

    def __getattr__(self, k):
        try:
            return self.__dict__.get("v", {})[k]
        except KeyError:
            raise AttributeError(k)

    def load(self):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        for k, _l, _d, lo, hi, kind in FIELDS:
            try:
                val = kind(data[k])
            except (KeyError, TypeError, ValueError):
                continue
            if lo <= val <= hi:
                self.v[k] = val

    def save(self):
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(self.v, f, indent=2)

    def update(self, raw: dict):
        novos = {}
        for k, label, _d, lo, hi, kind in FIELDS:
            txt = str(raw[k]).strip().replace(",", ".")
            try:
                val = int(float(txt)) if kind is int else float(txt)
            except ValueError:
                raise ValueError(f"'{label}': valor inválido ({txt!r}).")
            if not lo <= val <= hi:
                raise ValueError(f"'{label}' tem de estar entre {lo:g} e {hi:g}.")
            novos[k] = val
        self.v.update(novos)
        self.save()

    def reset(self):
        self.v = {k: d for k, _l, d, _lo, _hi, _t in FIELDS}
        self.save()


cfg = Settings()

CHAIN_ID = 8453  # Base
TOKENS = {
    "WETH": "0x4200000000000000000000000000000000000006",
    "cbBTC": "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",
}
ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "o", "type": "address"}], "name": "balanceOf",
     "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "to", "type": "address"}, {"name": "v", "type": "uint256"}],
     "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "s", "type": "address"}, {"name": "v", "type": "uint256"}],
     "name": "approve", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
]

# Uniswap V3 na Base (confirma estes endereços em docs.uniswap.org antes de usar fundos reais)
SWAP_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"   # SwapRouter02
QUOTER_V2 = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
V3_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
FEE_TIERS = (100, 500, 3000, 10000)

ROUTER_ABI = [{
    "name": "exactInputSingle", "type": "function", "stateMutability": "payable",
    "inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
        {"name": "fee", "type": "uint24"}, {"name": "recipient", "type": "address"},
        {"name": "amountIn", "type": "uint256"}, {"name": "amountOutMinimum", "type": "uint256"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"}]}],
    "outputs": [{"name": "amountOut", "type": "uint256"}],
}]
QUOTER_ABI = [{
    "name": "quoteExactInputSingle", "type": "function", "stateMutability": "nonpayable",
    "inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
        {"name": "amountIn", "type": "uint256"}, {"name": "fee", "type": "uint24"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"}]}],
    "outputs": [{"name": "amountOut", "type": "uint256"}, {"name": "sqrtPriceX96After", "type": "uint160"},
                {"name": "initializedTicksCrossed", "type": "uint32"}, {"name": "gasEstimate", "type": "uint256"}],
}]
FACTORY_ABI = [{
    "name": "getPool", "type": "function", "stateMutability": "view",
    "inputs": [{"name": "a", "type": "address"}, {"name": "b", "type": "address"}, {"name": "fee", "type": "uint24"}],
    "outputs": [{"name": "", "type": "address"}],
}]
POOL_ABI = [{
    "name": "liquidity", "type": "function", "stateMutability": "view",
    "inputs": [], "outputs": [{"name": "", "type": "uint128"}],
}]
LLAMA_KEYS = [f"base:{TOKENS['cbBTC']}", f"base:{TOKENS['WETH']}"]


# ---------- Carteira ----------
class WalletManager:
    def __init__(self, rpc: str):
        self.w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 20}))
        self.account = None
        self._fee = POOL_FEE or None
        self._decs = {}

    def importar(self, private_key: str, password: str | None = None) -> str:
        pk = private_key.strip()
        if not pk.startswith("0x"):
            pk = "0x" + pk
        self.account = Account.from_key(pk)
        if password:
            with open(KEYSTORE, "w") as f:
                json.dump(Account.encrypt(pk, password), f)
        return self.account.address

    def desbloquear(self, password: str) -> str:
        with open(KEYSTORE) as f:
            keystore = json.load(f)
        pk = Account.decrypt(keystore, password)
        self.account = Account.from_key(pk)
        return self.account.address

    def _contract(self, nome: str):
        return self.w3.eth.contract(address=Web3.to_checksum_address(TOKENS[nome]), abi=ERC20_ABI)

    def saldos(self) -> dict:
        addr = self.account.address
        out = {"ETH": self.w3.eth.get_balance(addr) / 1e18}
        for nome in TOKENS:
            c = self._contract(nome)
            out[nome] = c.functions.balanceOf(addr).call() / 10 ** c.functions.decimals().call()
        return out

    # ----- swaps (Uniswap V3) -----
    def _ck(self, addr: str) -> str:
        return Web3.to_checksum_address(addr)

    def _decimais(self, nome: str) -> int:
        if nome not in self._decs:
            self._decs[nome] = self._contract(nome).functions.decimals().call()
        return self._decs[nome]

    def saldo_bruto(self, nome: str) -> int:
        return self._contract(nome).functions.balanceOf(self.account.address).call()

    def verificar_contratos(self):
        """Falha cedo se algum endereço não tiver contrato (rede/endereço errado)."""
        for nome, addr in (("SwapRouter02", SWAP_ROUTER), ("QuoterV2", QUOTER_V2), ("Factory", V3_FACTORY)):
            if len(self.w3.eth.get_code(self._ck(addr))) == 0:
                raise RuntimeError(f"{nome} ({addr}) não tem contrato nesta rede. Confirma RPC_URL e endereços.")
        if self.w3.eth.chain_id != CHAIN_ID:
            raise RuntimeError(f"RPC não é a Base (chain_id {self.w3.eth.chain_id}).")

    def fee_tier(self) -> int:
        if self._fee:
            return self._fee
        factory = self.w3.eth.contract(address=self._ck(V3_FACTORY), abi=FACTORY_ABI)
        melhor, liq = None, -1
        for fee in FEE_TIERS:
            pool = factory.functions.getPool(self._ck(TOKENS["WETH"]), self._ck(TOKENS["cbBTC"]), fee).call()
            if int(pool, 16) == 0:
                continue
            liquidez = self.w3.eth.contract(address=pool, abi=POOL_ABI).functions.liquidity().call()
            if liquidez > liq:
                melhor, liq = fee, liquidez
        if melhor is None:
            raise RuntimeError("Nenhuma pool V3 WETH/cbBTC encontrada.")
        self._fee = melhor
        return melhor

    def _quote(self, de: str, para: str, valor_bruto: int) -> int:
        quoter = self.w3.eth.contract(address=self._ck(QUOTER_V2), abi=QUOTER_ABI)
        params = (self._ck(TOKENS[de]), self._ck(TOKENS[para]), valor_bruto, self.fee_tier(), 0)
        return quoter.functions.quoteExactInputSingle(params).call()[0]

    def preco_pool(self) -> float:
        """WETH por cbBTC executável na pool (cotação de 0.005 WETH, já com fee da pool)."""
        entrada = 5 * 10 ** 15
        saida = self._quote("WETH", "cbBTC", entrada)
        return (entrada / 1e18) / (saida / 10 ** self._decimais("cbBTC"))

    def valor_saida(self, cbbtc_bruto: int) -> int:
        """Quantos wei de WETH se receberiam agora ao vender `cbbtc_bruto` (cotação real da pool)."""
        return self._quote("cbBTC", "WETH", cbbtc_bruto)

    def _tx_base(self) -> dict:
        origem = self.account.address
        return {"from": origem, "nonce": self.w3.eth.get_transaction_count(origem, "pending"),
                "gasPrice": int(self.w3.eth.gas_price * 1.2), "chainId": CHAIN_ID}

    def garantir_allowance(self, nome: str, spender: str, valor: int):
        """Se a allowance for menor que `valor`, faz approve e ESPERA pelo receipt antes de continuar."""
        c = self._contract(nome)
        origem, spender = self.account.address, self._ck(spender)
        if c.functions.allowance(origem, spender).call() >= valor:
            return
        self._enviar(c.functions.approve(spender, valor).build_transaction(self._tx_base()))
        for _ in range(6):  # alguns RPCs demoram a refletir o estado novo
            if c.functions.allowance(origem, spender).call() >= valor:
                return
            time.sleep(1)
        raise RuntimeError(f"Approve de {nome} confirmado mas a allowance continua insuficiente.")

    def _diagnostico(self, nome: str, necessario: int) -> str:
        try:
            d = 10 ** self._decimais(nome)
            c = self._contract(nome)
            saldo = c.functions.balanceOf(self.account.address).call()
            allow = c.functions.allowance(self.account.address, self._ck(SWAP_ROUTER)).call()
            return f"[diagnóstico {nome}] saldo={saldo / d:.8f} allowance={allow / d:.8f} necessário={necessario / d:.8f}"
        except Exception as e:
            return f"[diagnóstico falhou: {e}]"

    def swap(self, de: str, para: str, valor_bruto: int) -> tuple[str, int]:
        """Swap exact-input no Uniswap V3. Devolve (tx_hash, quantidade_bruta_recebida)."""
        origem = self.account.address
        if valor_bruto <= 0:
            raise RuntimeError("Quantidade a trocar é 0.")
        saldo = self.saldo_bruto(de)
        if saldo < valor_bruto:
            d = 10 ** self._decimais(de)
            raise RuntimeError(f"Saldo de {de} insuficiente: tens {saldo / d:.8f}, precisas de {valor_bruto / d:.8f}.")
        if self.w3.eth.get_balance(origem) < MIN_ETH_GAS * 1e18:
            raise RuntimeError(f"ETH insuficiente para gas (mínimo {MIN_ETH_GAS} ETH).")

        fee = self.fee_tier()
        esperado = self._quote(de, para, valor_bruto)
        minimo = int(esperado * (1 - cfg.slippage_pct / 100))

        self.garantir_allowance(de, SWAP_ROUTER, valor_bruto)

        t_in, t_out = self._ck(TOKENS[de]), self._ck(TOKENS[para])
        antes = self.saldo_bruto(para)
        router = self.w3.eth.contract(address=self._ck(SWAP_ROUTER), abi=ROUTER_ABI)
        try:
            # build_transaction faz estimate_gas: se fosse reverter (STF, slippage...) falha aqui, sem gastar gas
            tx = router.functions.exactInputSingle((t_in, t_out, fee, origem, valor_bruto, minimo, 0)).build_transaction(
                self._tx_base())
            h = self._enviar(tx)
        except Exception as e:
            raise RuntimeError(f"{e} {self._diagnostico(de, valor_bruto)}") from e
        return h, self.saldo_bruto(para) - antes

    def _enviar(self, tx: dict) -> str:
        signed = self.account.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        h = self.w3.eth.send_raw_transaction(raw)
        hx = h.hex() if h.hex().startswith("0x") else "0x" + h.hex()
        rec = self.w3.eth.wait_for_transaction_receipt(h, timeout=180)
        if rec.status != 1:
            raise RuntimeError(f"Transação falhou: {hx}")
        return hx

    def enviar_tudo(self, destino: str) -> list[str]:
        w3 = self.w3
        destino = Web3.to_checksum_address(destino)
        origem = self.account.address
        nonce = w3.eth.get_transaction_count(origem)
        gas_price = int(w3.eth.gas_price * 1.2)
        msgs = []

        for nome in TOKENS:
            c = self._contract(nome)
            bruto = c.functions.balanceOf(origem).call()
            if bruto == 0:
                continue
            tx = c.functions.transfer(destino, bruto).build_transaction(
                {"from": origem, "nonce": nonce, "gasPrice": gas_price, "chainId": CHAIN_ID}
            )
            h = self._enviar(tx)
            nonce += 1
            msgs.append(f"{nome} enviado ({bruto / 10 ** c.functions.decimals().call():.8f}) tx {h}")

        eth = w3.eth.get_balance(origem)
        reserva = 21000 * gas_price + 30_000_000_000_000  # margem para a taxa L1 da Base
        if eth > reserva:
            valor = eth - reserva
            tx = {"to": destino, "value": valor, "gas": 21000, "gasPrice": gas_price,
                  "nonce": nonce, "chainId": CHAIN_ID}
            h = self._enviar(tx)
            msgs.append(f"ETH enviado ({valor / 1e18:.8f}) tx {h}")
        else:
            msgs.append("ETH: saldo insuficiente para cobrir taxas, nada enviado.")
        return msgs


# ---------- Dados, indicadores, Gemini ----------
def preco_ratio() -> float:
    """WETH por cbBTC (preço agregado DefiLlama; usado como alternativa quando a pool não responde)."""
    url = "https://coins.llama.fi/prices/current/" + ",".join(LLAMA_KEYS)
    coins = requests.get(url, timeout=10).json()["coins"]
    return coins[LLAMA_KEYS[0]]["price"] / coins[LLAMA_KEYS[1]]["price"]


def historico_llama(n: int, periodo_s: int) -> list[float]:
    """Últimos n preços WETH por cbBTC (DefiLlama, 1 ponto por minuto). Devolve [] se não houver dados."""
    minutos = max(1, round(periodo_s / 60))
    fim = int(time.time())
    url = ("https://coins.llama.fi/chart/" + ",".join(LLAMA_KEYS)
           + f"?start={fim - n * minutos * 60}&span={n}&period={minutos}m")
    coins = requests.get(url, timeout=8).json()["coins"]
    cb = sorted(coins[LLAMA_KEYS[0]]["prices"], key=lambda x: x["timestamp"])
    we = sorted(coins[LLAMA_KEYS[1]]["prices"], key=lambda x: x["timestamp"])
    k = min(len(cb), len(we))
    if k == 0:
        return []
    return [a["price"] / b["price"] for a, b in zip(cb[len(cb) - k:], we[len(we) - k:])]


def _rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    g = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    p = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = g / p.replace(0, float("nan"))
    return (100 - 100 / (1 + rs)).fillna(50.0)


def indicadores(precos) -> dict | None:
    """Pacote de indicadores (só preço: a pool não dá OHLC/volume) para o Gemini decidir."""
    if len(precos) < MIN_AMOSTRAS:
        return None
    s = pd.Series(list(precos), dtype=float)
    ult = float(s.iloc[-1])
    r = lambda x, n=4: round(float(x), n)

    rsi = _rsi(s)
    rsi_v = float(rsi.iloc[-1])
    rmin, rmax = rsi.rolling(14).min().iloc[-1], rsi.rolling(14).max().iloc[-1]
    stoch_rsi = (rsi_v - rmin) / (rmax - rmin) if rmax > rmin else 0.5

    ema9, ema21 = s.ewm(span=9).mean(), s.ewm(span=21).mean()
    ema50 = s.ewm(span=50).mean()
    macd_l = s.ewm(span=12).mean() - s.ewm(span=26).mean()
    macd_s = macd_l.ewm(span=9).mean()
    hist = macd_l - macd_s

    media20 = s.rolling(20).mean()
    dp20 = s.rolling(20).std()
    m20, d20 = float(media20.iloc[-1]), float(dp20.iloc[-1])
    boll = (ult - (m20 - 2 * d20)) / (4 * d20) if d20 > 0 else 0.5
    boll_larg = (4 * d20 / m20 * 100) if m20 else 0.0
    zscore = (ult - m20) / d20 if d20 > 0 else 0.0

    atr = float(s.diff().abs().rolling(14).mean().iloc[-1])
    ret = s.pct_change()
    vol20 = float(ret.rolling(20).std().iloc[-1]) * 100
    vol_ant = float(ret.iloc[-40:-20].std()) * 100 if len(s) >= 40 else vol20

    janela = s.iloc[-40:]
    topo, fundo = float(janela.max()), float(janela.min())
    pos_range = (ult - fundo) / (topo - fundo) if topo > fundo else 0.5

    def mom(n):
        return r((ult / float(s.iloc[-n - 1]) - 1) * 100) if len(s) > n else None

    # declive da regressão linear dos últimos 20 pontos (% por ponto) e força da tendência (R²)
    y = s.iloc[-20:].to_numpy()
    x = pd.Series(range(20), dtype=float).to_numpy()
    xm, ym = x.mean(), y.mean()
    sxx = ((x - xm) ** 2).sum()
    declive = ((x - xm) * (y - ym)).sum() / sxx
    ss_tot = ((y - ym) ** 2).sum()
    r2 = 1 - (((y - (ym + declive * (x - xm))) ** 2).sum() / ss_tot) if ss_tot > 0 else 0.0

    # sequência atual de subidas/descidas
    seq, sinal = 0, 0
    for v in ret.iloc[::-1].dropna():
        sg = 1 if v > 0 else -1 if v < 0 else 0
        if sg == 0 or (sinal and sg != sinal):
            break
        sinal, seq = sg, seq + 1

    return {
        "preco": ult,
        "rsi14": r(rsi_v, 2),
        "stoch_rsi": r(stoch_rsi, 3),                    # 0 = sobrevendido, 1 = sobrecomprado
        "ema9": float(ema9.iloc[-1]), "ema21": float(ema21.iloc[-1]), "ema50": float(ema50.iloc[-1]),
        "tendencia_curta": "alta" if ema9.iloc[-1] > ema21.iloc[-1] else "baixa",
        "tendencia_longa": "alta" if ult > ema50.iloc[-1] else "baixa",
        "dist_ema21_pct": r((ult / float(ema21.iloc[-1]) - 1) * 100),
        "macd": r(macd_l.iloc[-1] / ult * 100, 5), "macd_sinal": r(macd_s.iloc[-1] / ult * 100, 5),
        "macd_hist_pct": r(hist.iloc[-1] / ult * 100, 5),
        "macd_hist_anterior_pct": r(hist.iloc[-2] / ult * 100, 5),
        "bollinger_pos": r(boll, 3),                     # 0 = banda inferior, 1 = banda superior
        "bollinger_largura_pct": r(boll_larg, 3),
        "zscore_20": r(zscore, 2),
        "atr14_pct": r(atr / ult * 100, 5),
        "volatilidade_pct": r(vol20, 4), "volatilidade_anterior_pct": r(vol_ant, 4),
        "mom_3_pct": mom(3), "mom_10_pct": mom(10), "mom_20_pct": mom(20), "mom_40_pct": mom(39),
        "topo_40": topo, "fundo_40": fundo, "pos_no_range_40": r(pos_range, 3),
        "dist_topo_pct": r((ult / topo - 1) * 100), "dist_fundo_pct": r((ult / fundo - 1) * 100),
        "declive_20_pct_por_min": r(declive / ult * 100, 5), "forca_tendencia_r2": r(r2, 3),
        "sequencia_ticks": seq * sinal,                  # >0 subidas seguidas, <0 descidas seguidas
    }


_gemini = None
_gemini_key = None  # chave introduzida na janela (memória)
# Por defeito o Gemini PENSA antes de decidir (mais precisão). GEMINI_THINKING=0 desliga (mais rápido/barato).
_thinking_off = os.getenv("GEMINI_THINKING", "1").strip().lower() in ("0", "false", "no")


def _ler_chave_guardada() -> str | None:
    try:
        with open(GEMINI_KEY_FILE, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def obter_chave_gemini() -> str | None:
    """Ordem: chave da janela > gemini.key > GEMINI_API_KEY do .env."""
    return _gemini_key or _ler_chave_guardada() or os.getenv("GEMINI_API_KEY", "").strip() or None


def origem_chave_gemini() -> str:
    if _gemini_key:
        return "definida nesta sessão" + (" e guardada em gemini.key" if _ler_chave_guardada() == _gemini_key else "")
    if _ler_chave_guardada():
        return "guardada em gemini.key"
    if os.getenv("GEMINI_API_KEY", "").strip():
        return "vinda do .env"
    return "NÃO definida"


def definir_chave_gemini(chave: str, guardar: bool):
    global _gemini, _gemini_key
    chave = chave.strip()
    if not chave:
        raise ValueError("Cola primeiro a chave do Gemini.")
    _gemini_key, _gemini = chave, None
    _bloqueados.clear()
    if guardar:  # ficheiro só legível pelo utilizador (em Windows o chmod é ignorado)
        fd = os.open(GEMINI_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(chave)


def apagar_chave_gemini():
    global _gemini, _gemini_key
    _gemini_key, _gemini = None, None
    try:
        os.remove(GEMINI_KEY_FILE)
    except OSError:
        pass


def _cliente():
    global _gemini
    if _gemini is None:
        chave = obter_chave_gemini()
        if not chave:
            raise RuntimeError("Falta a chave do Gemini (cola-a na janela, em gemini.key ou GEMINI_API_KEY no .env).")
        _gemini = genai.Client(api_key=chave)
    return _gemini


def testar_gemini() -> str:
    out = _gerar('Responde APENAS com JSON {"ok":true}')
    modelo = out.pop("_modelo", "?")
    out.pop("_eventos", None)
    return f"{json.dumps(out)} (modelo: {modelo}; rotação: {', '.join(_ordem_modelos())})"


PROMPT_SISTEMA = (
    "És um trader profissional de elevada skill a gerir sozinho uma carteira on-chain na rede Base. "
    "Operas o par cbBTC/WETH no Uniswap V3 (preço = WETH por cbBTC). A conta está sempre em WETH quando "
    "não há posição; comprar = WETH->cbBTC, vender = cbBTC->WETH. Só podes estar comprado (sem alavancagem, "
    "sem short). Tens total liberdade e responsabilidade: NÃO existem take profit, stop loss, trailing nem "
    "limiares fixos de indicadores. Tu decides quando entrar, quando sair e que fração do capital usar. "
    "Princípios: preservar capital vem primeiro; só operas quando há vantagem estatística clara depois de "
    "custos (fee da pool de ida e volta + gas + slippage); 'esperar' é uma decisão válida e frequente; "
    "corta perdas cedo quando a tese deixa de ser válida e deixa correr ganhos enquanto a tendência se mantém; "
    "ajusta o tamanho à convicção e à volatilidade; não persigas preço nem faças revenge trading; "
    "aprende com o histórico recente de trades que te é dado. "
    'Responde APENAS com JSON: {"analise":"o teu raciocínio passo a passo em 2-5 frases: leitura da '
    'tendência, momentum, volatilidade, risco/retorno e porque agora","acao":"comprar|vender|esperar",'
    '"tamanho_pct":1-100,"confianca":0-1,"motivo":"decisão em max 120 caracteres",'
    '"plano":"o que esperas que aconteça / o que invalida a tese, max 160 caracteres",'
    '"ajustes":{"min_conf":0.2-0.95,"cooldown_s":0-3600,"proximo_ciclo_s":10-600}}. '
    "Vês o saldo real da carteira (ETH para gas, WETH livre, cbBTC) e os limites fixos de segurança, que não "
    "podes alterar. Também controlas os teus próprios parâmetros: a confiança mínima para executares compras "
    "(sobe-a quando o mercado está confuso ou vens de perdas, baixa-a quando a leitura é limpa), o cooldown "
    "após fechar e o intervalo até à próxima análise (curto com posição aberta ou mercado rápido, longo quando "
    "não há nada a fazer). Inclui-os em \"ajustes\". "
    "Os pedidos à API são limitados (plano gratuito): usa intervalos longos (120-600 s) quando não há posição nem "
    "oportunidade clara, e reserva ciclos curtos para posição aberta ou setups claros. "
    "'comprar' só é válido sem posição aberta e 'vender' só com posição aberta; tamanho_pct = % do máximo "
    "permitido por ordem (só conta ao comprar; vender fecha sempre toda a posição)."
)


class GeminiEsgotado(Exception):
    """Todos os modelos estão esgotados/indisponíveis. `ate` = quando o primeiro volta a poder ser usado."""

    def __init__(self, ate: float, eventos: list):
        super().__init__("todos os modelos Gemini esgotados/indisponíveis")
        self.ate, self.eventos = ate, eventos


_bloqueados: dict = {}      # modelo -> timestamp até quando está de fora (quota/erro)
_modelos_cache = None
_sem_thinking_cfg: set = set()
_EXCLUIR = ("embedding", "tts", "image", "imagen", "live", "audio", "veo", "robotics", "computer-use",
            "aqa", "-latest", "vision", "customtools", "learnlm")


def _prioridade(nome: str):
    n = nome.lower()
    v = re.findall(r"\d+(?:\.\d+)?", n)
    versao = float(v[0]) if v else 0.0
    if "exp" in n:
        grupo = 5
    elif n.startswith("gemma"):
        grupo = 4
    elif "pro" in n:
        grupo = 3
    elif "lite" in n:
        grupo = 2
    elif "flash" in n:
        grupo = 0
    else:
        grupo = 1
    return (grupo, -versao, n)


def listar_modelos_gemini() -> list[str]:
    """Modelos de texto da API (descobertos na própria API). GEMINI_MODELS no .env força uma lista."""
    global _modelos_cache
    if _modelos_cache is not None:
        return _modelos_cache
    forcados = [m.strip() for m in os.getenv("GEMINI_MODELS", "").split(",") if m.strip()]
    if forcados:
        _modelos_cache = forcados
        return forcados
    achados = []
    try:
        for m in _cliente().models.list():
            nome = (getattr(m, "name", "") or "").replace("models/", "")
            acoes = getattr(m, "supported_actions", None) or []
            if "generateContent" not in acoes or not nome.startswith(("gemini", "gemma")):
                continue
            if any(x in nome.lower() for x in _EXCLUIR):
                continue
            achados.append(nome)
    except Exception:
        return [GEMINI_MODEL]   # não guarda em cache: tenta de novo mais tarde
    achados.sort(key=_prioridade)
    lista = [GEMINI_MODEL] + [m for m in achados if m != GEMINI_MODEL]
    _modelos_cache = lista
    return lista


def _ordem_modelos() -> list[str]:
    return listar_modelos_gemini()


def _parse_delay(txt: str):
    m = re.search(r"retrydelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", txt, re.I)
    if m:
        return float(m.group(1))
    m = re.search(r"retry in\s+((?:\d+h)?(?:\d+m)?(?:[\d.]+s)?)", txt, re.I)
    if m and m.group(1):
        t = 0.0
        for val, un in re.findall(r"([\d.]+)([hms])", m.group(1)):
            t += float(val) * {"h": 3600, "m": 60, "s": 1}[un]
        return t
    return None


def _classificar_erro(e: Exception):
    """(segundos_de_bloqueio, motivo) se o erro é só deste modelo; None se é fatal (chave inválida, etc.)."""
    txt = str(e)
    low = txt.lower()
    if "api key" in low or "api_key" in low or "401" in txt or "unauthenticated" in low:
        return None
    if "429" in txt or "resource_exhausted" in low or "quota" in low:
        delay = _parse_delay(txt)
        if "limit: 0" in low:
            delay = max(delay or 0, 86400)          # este modelo não tem quota grátis
        elif delay is None:
            delay = 65                              # limite por minuto
        return min(delay, 90000), "quota esgotada"
    if any(x in low for x in ("503", "unavailable", "overloaded", "500", "internal", "504", "deadline")):
        return 60, "indisponível"
    if any(x in low for x in ("404", "not_found", "not found", "not supported", "does not support",
                              "unsupported", "permission_denied")):
        return 86400, "não disponível para esta chave"
    return None


def _extrair_json(texto: str) -> dict:
    try:
        out = json.loads(texto)
    except ValueError:
        m = re.search(r"\{.*\}", texto or "", re.S)
        if not m:
            raise
        out = json.loads(m.group(0))
    if isinstance(out, list):
        out = out[0] if out else {}
    return out if isinstance(out, dict) else {}


def _chamar_modelo(modelo: str, prompt: str) -> dict:
    config = {} if modelo.startswith("gemma") else {"response_mime_type": "application/json"}
    if _thinking_off and not modelo.startswith("gemma") and modelo not in _sem_thinking_cfg:
        config["thinking_config"] = {"thinking_budget": 0}
    for _ in range(3):
        try:
            r = _cliente().models.generate_content(model=modelo, contents=prompt, config=config or None)
            return _extrair_json(r.text)
        except Exception as e:
            low = str(e).lower()
            if "thinking" in low and "thinking_config" in config:
                config.pop("thinking_config")
                _sem_thinking_cfg.add(modelo)
            elif ("json mode" in low or "mime" in low) and "response_mime_type" in config:
                config.pop("response_mime_type")
            else:
                raise
    raise RuntimeError("config inválida para " + modelo)


def _gerar(prompt: str) -> dict:
    """Corre os modelos por ordem; quando um esgota passa ao seguinte. Anexa _modelo e _eventos ao resultado."""
    eventos = []
    agora = time.time()
    for modelo in _ordem_modelos():
        if _bloqueados.get(modelo, 0) > time.time():
            continue
        try:
            out = _chamar_modelo(modelo, prompt)
        except Exception as e:
            info = _classificar_erro(e)
            if info is None:
                raise
            delay, motivo = info
            ate = time.time() + delay
            _bloqueados[modelo] = ate
            m = re.search(r"['\"]?model['\"]?\s*:\s*['\"]?([\w.\-]+)", str(e))
            if m:                                   # o alias (ex.: *-latest) partilha a quota do modelo real
                _bloqueados[m.group(1)] = max(_bloqueados.get(m.group(1), 0), ate)
            quando = datetime.fromtimestamp(ate).strftime("%d/%m %H:%M")
            eventos.append(f"[GEMINI] modelo {modelo}: {motivo}; fora de rotação até {quando}.")
            continue
        out["_modelo"], out["_eventos"] = modelo, eventos
        return out
    ate = min((_bloqueados.get(m, agora + 3600) for m in _ordem_modelos()), default=agora + 3600)
    raise GeminiEsgotado(max(ate, agora + 30), eventos)


def pedir_decisao_gemini(contexto: dict) -> dict:
    prompt = PROMPT_SISTEMA + "\n\nEstado atual (JSON):\n" + json.dumps(contexto, ensure_ascii=False)
    return _gerar(prompt)


# ---------- Bot (SIMULAÇÃO ou REAL) ----------
class Bot:
    def __init__(self, log, wallet: "WalletManager | None" = None):
        self.log = log
        self.wallet = wallet
        self.live = LIVE_TRADING
        self.lock = threading.RLock()  # serializa ciclo, abrir/fechar e fecho manual/ao parar
        self.running = False
        self.posicao = None
        self.precos = deque(maxlen=300)
        self.tempos = deque(maxlen=300)       # timestamp de cada preço (para o Gemini saber o espaçamento)
        self.carteira = None                  # últimos saldos reais lidos (REAL)
        self._avisou_cart = False
        self.sincronizar_ajustes()
        self.var_atual = None
        self.ultimo_fecho = 0.0
        self.gemini_ate = 0.0
        self.modelo_atual = None
        self.gemini_falha_desde = None        # desde quando o Gemini não responde (para a proteção com posição aberta)
        self.hist_trades = deque(maxlen=10)   # últimos trades fechados, mostrados ao Gemini para aprender
        self._avisou_fonte = False
        self._reset_conta()

    def sincronizar_ajustes(self):
        """Parâmetros que o Gemini pode ajustar em runtime; arrancam com os valores da janela/.env."""
        self.min_conf_atual = cfg.min_conf
        self.cooldown_atual = cfg.cooldown_s
        self.loop_atual = cfg.loop_s

    def _aplicar_ajustes(self, aj):
        if not isinstance(aj, dict):
            return
        antes = (self.min_conf_atual, self.cooldown_atual, self.loop_atual)
        try:
            if aj.get("min_conf") is not None:
                self.min_conf_atual = max(0.2, min(0.95, float(aj["min_conf"])))
            if aj.get("cooldown_s") is not None:
                self.cooldown_atual = int(max(0, min(3600, float(aj["cooldown_s"]))))
            if aj.get("proximo_ciclo_s") is not None:
                self.loop_atual = int(max(10, min(600, float(aj["proximo_ciclo_s"]))))
        except (TypeError, ValueError):
            return
        if (self.min_conf_atual, self.cooldown_atual, self.loop_atual) != antes:
            self.log(f"[GEMINI] ajustou: confiança mín. {self.min_conf_atual:.2f} | cooldown {self.cooldown_atual}s | "
                     f"próxima análise em {self.loop_atual}s")

    def _reset_conta(self):
        self.capital = cfg.capital
        self.saldo = self.capital
        self.trades = 0
        self.vitorias = 0
        self.perdas_seguidas = 0

    # --- controlo ---
    def iniciar(self, live: bool | None = None):
        if self.running:
            return
        with self.lock:
            novo_modo = live is not None and live != self.live
            if novo_modo and self.posicao:
                self.log("Fecha a posição aberta antes de mudar de modo.")
                return
            if novo_modo:
                self.live = live
            if self.live and not (self.wallet and self.wallet.account):
                self.log("Modo REAL: importa ou desbloqueia a carteira antes de iniciar.")
                return
            if not self.posicao and (novo_modo or abs(cfg.capital - self.capital) > 1e-12):
                self._reset_conta()
            if self.live and not self.posicao:
                self._carregar_estado()
            self.sincronizar_ajustes()
            self.running = True
        threading.Thread(target=self._run, daemon=True).start()
        modo = "REAL" if self.live else "SIMULAÇÃO"
        self.log(f"Bot iniciado ({modo}): capital {self.capital} WETH, máx. {cfg.order_pct:g}% por ordem, "
                 f"o GEMINI decide tudo (sem TP/SL fixos). Ciclo a cada {cfg.loop_s}s.")

    def parar(self):
        self.running = False
        threading.Thread(target=self._fechar_ao_parar, daemon=True).start()  # swap real não pode bloquear a UI

    def fechar_manual(self):
        threading.Thread(target=self._fechar_seguro, args=("fecho manual",), daemon=True).start()

    def _fechar_ao_parar(self):
        self._fechar_seguro("bot parado")
        self.log("Bot parado.")

    def _fechar_seguro(self, motivo: str):
        with self.lock:
            if not self.posicao:
                self.log("Não há posição aberta.")
                return
            ratio = self.precos[-1] if self.precos else self.posicao["entrada"]
            try:
                res = self.fechar(ratio, motivo)
                self.log(f"Posição fechada ({motivo}): {res:+.6f} WETH (saldo {self.saldo:.6f})")
            except Exception as e:
                self.log(f"ERRO ao fechar posição ({motivo}); continua ABERTA: {e}")

    # --- estado em disco (só REAL) ---
    def _guardar_estado(self):
        if not self.live:
            return
        try:
            if self.posicao:
                with open(STATE_FILE, "w", encoding="utf-8") as f:
                    json.dump({"carteira": self.wallet.account.address, "posicao": self.posicao,
                               "saldo": self.saldo, "capital": self.capital}, f)
            elif os.path.exists(STATE_FILE):
                os.remove(STATE_FILE)
        except Exception as e:
            self.log(f"Aviso: não consegui guardar estado.json: {e}")

    def _carregar_estado(self):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        if d.get("carteira") != self.wallet.account.address or not d.get("posicao"):
            return
        self.posicao = d["posicao"]
        self.saldo = float(d.get("saldo", self.saldo))
        self.capital = float(d.get("capital", self.capital))
        self.log(f"Posição REAL retomada de estado.json ({self.posicao['cbbtc'] / 1e8:.8f} cbBTC).")

    def _registar_trade(self, p: dict, novo: float, res: float, motivo: str):
        try:
            novo_ficheiro = not os.path.exists(TRADES_CSV)
            with open(TRADES_CSV, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if novo_ficheiro:
                    w.writerow(["data", "modo", "entrada_weth_por_cbbtc", "tamanho_weth", "saida_weth",
                                "resultado_weth", "motivo"])
                w.writerow([datetime.now().isoformat(timespec="seconds"), "REAL" if self.live else "SIM",
                            f"{p['entrada']:.8f}", f"{p['tamanho']:.8f}", f"{novo:.8f}", f"{res:.8f}", motivo])
        except Exception as e:
            self.log(f"Aviso: não consegui escrever trades.csv: {e}")

    # --- operações ---
    def _base_ordem(self) -> float:
        """WETH disponível para novas ordens. REAL: nunca mais do que existe na carteira."""
        if self.live and self.carteira:
            return min(self.saldo, self.carteira["WETH"])
        return self.saldo

    def abrir(self, ratio, frac: float = 1.0):
        """frac = fração (0-1) do máximo por ordem (cfg.order_pct do saldo) decidida pelo Gemini."""
        with self.lock:
            tamanho = self._base_ordem() * cfg.order_pct / 100 * max(0.01, min(1.0, frac))
            agora = time.time()
            if not self.live:
                self.posicao = {"entrada": ratio, "tamanho": tamanho, "pico": 0.0, "aberta_em": agora}
            else:
                wei = min(int(tamanho * 10 ** 18), self.wallet.saldo_bruto("WETH"))  # nunca mais do que existe
                if wei <= 0:
                    raise RuntimeError("Sem WETH na carteira para abrir posição.")
                h, recebido = self.wallet.swap("WETH", "cbBTC", wei)
                if recebido <= 0:
                    raise RuntimeError(f"Swap de compra sem cbBTC recebido (tx {h}).")
                efetivo = (wei / 1e18) / (recebido / 1e8)  # WETH por cbBTC, já com fee e slippage
                self.posicao = {"entrada": efetivo, "tamanho": wei / 1e18, "cbbtc": recebido, "pico": 0.0,
                                "aberta_em": agora}
                self.log(f"[LIVE] Comprou {recebido / 1e8:.8f} cbBTC por {wei / 1e18:.6f} WETH, tx {h}")
            self.var_atual = None
            self._guardar_estado()

    def fechar(self, ratio, motivo: str = "") -> float:
        with self.lock:
            p = self.posicao
            if self.live:
                bruto = min(p["cbbtc"], self.wallet.saldo_bruto("cbBTC"))  # nunca vender mais do que existe
                h, weth_out = self.wallet.swap("cbBTC", "WETH", bruto)
                novo = weth_out / 1e18
                self.log(f"[LIVE] Vendeu {bruto / 1e8:.8f} cbBTC por {novo:.6f} WETH, tx {h}")
            else:
                novo = p["tamanho"] * (ratio / p["entrada"]) * (1 - FEE_PCT) ** 2
            res = novo - p["tamanho"]
            self.saldo += res
            self.posicao = None
            self.var_atual = None
            self.trades += 1
            if res > 0:
                self.vitorias += 1
                self.perdas_seguidas = 0
            else:
                self.perdas_seguidas += 1
            self.ultimo_fecho = time.time()
            self.hist_trades.append({
                "resultado_pct": round(res / p["tamanho"] * 100, 3), "motivo": motivo,
                "duracao_min": round((time.time() - p.get("aberta_em", time.time())) / 60, 1)})
            self._registar_trade(p, novo, res, motivo)
            self._guardar_estado()
            return res

    def _var_liquida(self, ratio) -> float:
        """Variação da posição já líquida de fees (REAL: cotação de saída na pool)."""
        p = self.posicao
        if self.live:
            return self.wallet.valor_saida(p["cbbtc"]) / 1e18 / p["tamanho"] - 1
        return ratio / p["entrada"] * (1 - FEE_PCT) ** 2 - 1

    def _preco(self) -> float:
        if self.wallet is not None:
            try:
                return self.wallet.preco_pool()
            except Exception as e:
                if not self._avisou_fonte:
                    self.log(f"Preço da pool indisponível ({e}); a usar DefiLlama.")
                    self._avisou_fonte = True
        return preco_ratio()

    def _contexto(self, ratio: float, ind: dict) -> dict:
        ctx = {
            "par": "cbBTC/WETH", "preco_weth_por_cbbtc": ratio, "indicadores": ind,
            "ultimos_30_precos": [round(x, 8) for x in list(self.precos)[-30:]],
            "custo_ida_e_volta_pct": round(2 * FEE_PCT * 100, 3),
            "slippage_max_pct": cfg.slippage_pct,
            "conta": {"capital_inicial_weth": self.capital, "saldo_weth": round(self.saldo, 8),
                      "resultado_pct": round((self.saldo / self.capital - 1) * 100, 3) if self.capital else 0,
                      "trades": self.trades, "vitorias": self.vitorias,
                      "perdas_seguidas": self.perdas_seguidas,
                      "perda_total_max_pct": cfg.max_loss_pct},
            "max_por_ordem_weth": round(self._base_ordem() * cfg.order_pct / 100, 8),
            "historico_ultimos_trades": list(self.hist_trades),
            "carteira": self._ctx_carteira(ratio),
            "parametros_atuais_ajustaveis_por_ti": {
                "min_conf": self.min_conf_atual, "cooldown_s": self.cooldown_atual,
                "proximo_ciclo_s": self.loop_atual},
            "limites_fixos_nao_alteraveis": {
                "perda_total_max_pct": cfg.max_loss_pct, "slippage_max_pct": cfg.slippage_pct,
                "max_pct_do_saldo_por_ordem": cfg.order_pct,
                "perdas_seguidas_max": cfg.max_losses or "desligado"},
            "dados": self._ctx_dados(),
            "posicao": None,
        }
        p = self.posicao
        if p:
            var = self.var_atual if self.var_atual is not None else 0.0
            ctx["posicao"] = {
                "entrada": p["entrada"], "tamanho_weth": round(p["tamanho"], 8),
                "resultado_liquido_atual_pct": round(var * 100, 3),
                "pico_resultado_pct": round(p.get("pico", 0.0) * 100, 3),
                "minutos_aberta": round((time.time() - p.get("aberta_em", time.time())) / 60, 1)}
        return ctx

    def _ctx_carteira(self, ratio: float) -> dict:
        p = self.posicao
        if self.live and self.carteira:
            c = self.carteira
            total = c["WETH"] + c["cbBTC"] * ratio
            return {"modo": "REAL", "eth_para_gas": round(c["ETH"], 6), "weth_livre": round(c["WETH"], 8),
                    "cbbtc": round(c["cbBTC"], 8), "valor_total_em_weth": round(total, 8),
                    "gas_minimo_eth": MIN_ETH_GAS}
        livre = self.saldo - (p["tamanho"] if p else 0.0)
        return {"modo": "SIMULACAO", "weth_livre": round(livre, 8),
                "em_posicao_weth": round(p["tamanho"], 8) if p else 0.0,
                "valor_total_em_weth": round(self.saldo, 8)}

    def _ctx_dados(self) -> dict:
        t = list(self.tempos)[-21:]
        gaps = [b - a for a, b in zip(t, t[1:]) if b > a]
        if not gaps:
            return {"pontos": len(self.precos)}
        gaps.sort()
        med = gaps[len(gaps) // 2]
        return {"pontos": len(self.precos), "segundos_por_ponto": round(med),
                "espacamento_irregular": gaps[-1] > 2.5 * max(gaps[0], 1)}

    def _ler_carteira(self):
        self.carteira = self.wallet.saldos()

    def _gemini(self, ctx: dict) -> dict | None:
        if time.time() < self.gemini_ate:
            return None
        try:
            out = pedir_decisao_gemini(ctx)
        except GeminiEsgotado as e:
            for ev in e.eventos:
                self.log(ev)
            self.gemini_ate = e.ate
            self.gemini_falha_desde = self.gemini_falha_desde or time.time()
            quando = datetime.fromtimestamp(e.ate).strftime("%d/%m %H:%M")
            self.log(f"Gemini: todos os modelos gratuitos esgotados/indisponíveis. Próxima tentativa às {quando}."
                     + (" A posição aberta mantém-se (proteção ao fim de "
                        f"{GEMINI_FALHA_FECHAR_MIN:g} min)." if self.posicao and GEMINI_FALHA_FECHAR_MIN else ""))
            return None
        except Exception as e:  # chave em falta/inválida, JSON inválido, rede...
            self.gemini_ate = time.time() + 120
            self.gemini_falha_desde = self.gemini_falha_desde or time.time()
            self.log(f"Gemini indisponível ({str(e)[:300]}); sem decisões durante 2 min.")
            return None
        for ev in out.pop("_eventos", []):
            self.log(ev)
        mod = out.pop("_modelo", None)
        if mod != self.modelo_atual:
            self.log(f"[GEMINI] a usar o modelo {mod}")
        self.modelo_atual = mod
        self.gemini_falha_desde = None
        return out

    def _pensar(self, gem: dict):
        """Mostra no log o raciocínio do Gemini."""
        self.log(f"[GEMINI] decisão: {str(gem.get('acao', '?')).upper()} | confiança {gem.get('confianca', '?')} | "
                 f"tamanho {gem.get('tamanho_pct', '-')}% | {gem.get('motivo', '')} (modelo {self.modelo_atual})")
        if gem.get("analise"):
            self.log(f"[GEMINI] análise: {gem['analise']}")
        if gem.get("plano"):
            self.log(f"[GEMINI] plano: {gem['plano']}")

    def _pode_entrar(self) -> bool:
        return time.time() - self.ultimo_fecho >= self.cooldown_atual

    def _amostrar(self) -> float:
        ratio = self._preco()
        self.precos.append(ratio)
        self.tempos.append(time.time())
        return ratio

    def _aquecer(self):
        """Arranque rápido: histórico (DefiLlama) e preço da pool pedidos EM PARALELO."""
        if len(self.precos) >= MIN_AMOSTRAS:
            return
        t0 = time.time()
        try:
            with ThreadPoolExecutor(2) as ex:
                f_hist = ex.submit(historico_llama, 70, 60)   # 1 ponto por minuto
                f_preco = ex.submit(self._preco)
                hist, preco = f_hist.result(), f_preco.result()
            if len(hist) < MIN_AMOSTRAS:
                raise RuntimeError(f"só {len(hist)} pontos")
            fator = preco / hist[-1]  # alinha o histórico (preço médio) com o preço da pool
            agora = time.time()
            self.precos.clear()
            self.tempos.clear()
            for i, h in enumerate(hist):
                self.precos.append(h * fator)
                self.tempos.append(agora - (len(hist) - 1 - i) * 60)
            self.log(f"Histórico carregado ({len(hist)} pontos) em {time.time() - t0:.1f}s: indicadores prontos.")
        except Exception as e:
            self.log(f"Sem histórico ({e}); modo rápido: {MIN_AMOSTRAS} amostras, 1 a cada {AMOSTRA_RAPIDA_S}s "
                     f"(~{MIN_AMOSTRAS * AMOSTRA_RAPIDA_S}s).")
            self.precos.clear()
            self.tempos.clear()
            falhas = 0
            while self.running and len(self.precos) < MIN_AMOSTRAS and falhas < 10:
                try:
                    self._amostrar()
                except Exception as e2:
                    falhas += 1
                    self.log(f"Erro a recolher amostra ({e2})")
                if len(self.precos) % 10 == 0 and self.precos:
                    self.log(f"A recolher dados ({len(self.precos)}/{MIN_AMOSTRAS})")
                time.sleep(AMOSTRA_RAPIDA_S)

    def _run(self):
        ex = None
        f_ver = None
        if self.live:  # verificações on-chain em paralelo com o histórico
            ex = ThreadPoolExecutor(1)
            f_ver = ex.submit(lambda: (self.wallet.verificar_contratos(), self.wallet.fee_tier(),
                                       self._ler_carteira()))
        self._aquecer()
        if f_ver is not None:
            try:
                f_ver.result()
            except Exception as e:
                self.log(f"Verificação inicial falhou, bot parado: {e}")
                self.running = False
                return
            finally:
                ex.shutdown(wait=False)
        while self.running:
            try:
                with self.lock:
                    self._ciclo()
            except Exception as e:  # rede, RPC, etc.
                self.log(f"Erro no ciclo: {e}")
            for _ in range(self.loop_atual):
                if not self.running:
                    break
                time.sleep(1)

    def _ciclo(self):
        with ThreadPoolExecutor(2) as ex:   # preço da pool e saldos da carteira em paralelo
            f_p = ex.submit(self._preco)
            f_c = ex.submit(self._ler_carteira) if self.live else None
            ratio = f_p.result()
            if f_c is not None:
                try:
                    f_c.result()
                except Exception as e:
                    if not self._avisou_cart:
                        self.log(f"Aviso: não consegui ler os saldos da carteira ({e}).")
                        self._avisou_cart = True
        self.precos.append(ratio)
        self.tempos.append(time.time())
        ind = indicadores(self.precos)
        if ind is None:
            self.log(f"A recolher dados ({len(self.precos)}/{MIN_AMOSTRAS})")
            return
        tag = "LIVE" if self.live else "SIM"
        p = self.posicao
        if p:
            var = self._var_liquida(ratio)
            self.var_atual = var
            p["pico"] = max(p.get("pico", 0.0), var)
            # Proteção da conta (não é regra de trading): perda total, incluindo a posição aberta.
            equity = self.saldo + var * p["tamanho"]
            if equity < self.capital * (1 - cfg.max_loss_pct / 100):
                res = self.fechar(ratio, "kill switch (perda total máx.)")
                self.running = False
                self.log(f"KILL SWITCH: perda total máxima atingida; posição fechada ({res:+.6f} WETH). Bot parado.")
                return

        gem = self._gemini(self._contexto(ratio, ind))
        if (gem is None and self.posicao and GEMINI_FALHA_FECHAR_MIN and self.gemini_falha_desde
                and time.time() - self.gemini_falha_desde >= GEMINI_FALHA_FECHAR_MIN * 60):
            res = self.fechar(ratio, "Gemini indisponível (proteção)")
            self.log(f"[{tag}] Fechou posição (Gemini indisponível há {GEMINI_FALHA_FECHAR_MIN:g} min): "
                     f"{res:+.6f} WETH (saldo {self.saldo:.6f})")
        if gem and self.running:
            self._pensar(gem)
            self._aplicar_ajustes(gem.get("ajustes"))
            acao = str(gem.get("acao", "esperar")).strip().lower()
            try:
                conf = float(gem.get("confianca", 0))
            except (TypeError, ValueError):
                conf = 0.0
            try:
                frac = float(gem.get("tamanho_pct", 100)) / 100
            except (TypeError, ValueError):
                frac = 1.0
            if self.posicao and acao == "vender":
                motivo = "decisão do Gemini: " + str(gem.get("motivo", ""))[:120]
                res = self.fechar(ratio, motivo)
                self.log(f"[{tag}] Fechou posição ({motivo}): {res:+.6f} WETH (saldo {self.saldo:.6f})")
            elif not self.posicao and acao == "comprar":
                if conf < self.min_conf_atual:
                    self.log(f"[{tag}] Compra ignorada: confiança {conf:.2f} < mínimo {self.min_conf_atual:.2f}.")
                elif not self._pode_entrar():
                    self.log(f"[{tag}] Compra ignorada: cooldown de {self.cooldown_atual}s ainda ativo.")
                else:
                    self.abrir(ratio, frac)
                    self.log(f"[{tag}] Abriu posição a {self.posicao['entrada']:.6f} "
                             f"({self.posicao['tamanho']:.6f} WETH, Gemini {conf:.2f})")
            elif acao in ("comprar", "vender"):
                self.log(f"[{tag}] Ação '{acao}' ignorada (já {'tens' if self.posicao else 'não tens'} posição).")

        if self.saldo < self.capital * (1 - cfg.max_loss_pct / 100):
            self.running = False
            self.log("KILL SWITCH: perda máxima atingida. Bot parado.")
        elif cfg.max_losses and self.perdas_seguidas >= cfg.max_losses and not self.posicao:
            self.running = False
            self.log(f"Pausa: {self.perdas_seguidas} perdas seguidas. Bot parado para rever.")


# ---------- Interface ----------
class App(tk.Tk):
    # paleta DeFiAnts (roxo e ciano neon sobre fundo escuro)
    BG, PAINEL, BORDA = "#0b0620", "#140c33", "#4c2a9a"
    TEXTO, SUAVE = "#e8defc", "#9a88c9"
    ROXO, CIANO = "#a855f7", "#22d3ee"
    VERDE, VERMELHO, LARANJA = "#15803d", "#b3261e", "#c25e00"   # fundo dos botões
    TXT_OK, TXT_ERR, TXT_LIVE = "#4ade80", "#ff6b81", "#fbbf24"   # cor do texto

    def __init__(self):
        super().__init__()
        self.title("DeFiAnts · AI Trader cbBTC/WETH")
        self.geometry("860x1030")
        self.configure(bg=self.BG)
        self._tema()
        self.q = queue.Queue()
        self.wallet = WalletManager(RPC_URL)
        self.bot = Bot(self.log, self.wallet)
        self._build()
        self.after(200, self._poll)
        self.after(2000, self._refresh_status)

    def _tema(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        campo = "#1c1145"
        st.configure(".", background=self.PAINEL, foreground=self.TEXTO)
        st.configure("TFrame", background=self.PAINEL)
        st.configure("TLabel", background=self.PAINEL, foreground=self.TEXTO)
        st.configure("TLabelframe", background=self.PAINEL, bordercolor=self.BORDA,
                     lightcolor=self.BORDA, darkcolor=self.BORDA)
        st.configure("TLabelframe.Label", background=self.BG, foreground=self.ROXO, font=("Consolas", 10, "bold"))
        st.configure("TEntry", fieldbackground=campo, foreground=self.TEXTO, insertcolor=self.TEXTO,
                     bordercolor=self.BORDA, lightcolor=self.BORDA, darkcolor=self.BORDA)
        st.configure("TButton", background="#2a1660", foreground=self.TEXTO, bordercolor=self.BORDA,
                     focuscolor=self.PAINEL, padding=4)
        st.map("TButton", background=[("active", self.BORDA)])
        st.configure("TCheckbutton", background=self.PAINEL, foreground=self.TEXTO)
        st.map("TCheckbutton", background=[("active", self.PAINEL)],
               indicatorcolor=[("selected", self.CIANO), ("!selected", campo)])

    def _carregar_logo(self):
        if not os.path.exists(LOGO_FILE):
            return None
        try:
            from PIL import Image, ImageTk
            img = Image.open(LOGO_FILE)
            img.thumbnail((110, 110))
            return ImageTk.PhotoImage(img)
        except Exception:
            try:  # sem Pillow: o Tk reduz por fatores inteiros
                img = tk.PhotoImage(file=LOGO_FILE)
                f = max(1, img.width() // 110)
                return img.subsample(f, f)
            except Exception:
                return None

    # --- infraestrutura ---
    def log(self, txt):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.q.put(lambda: self._append(f"[{stamp}] {txt}\n"))

    def _append(self, txt):
        if re.search(r"ERRO|Erro|KILL|Pausa|falhou|Fechou.*?: -", txt):
            tag = "err"
        elif re.search(r"Fechou.*?: \+", txt):
            tag = "ok"
        elif "[GEMINI]" in txt:
            tag = "think"
        elif "[LIVE]" in txt:
            tag = "live"
        else:
            tag = None
        self.console.configure(state="normal")
        if tag:
            self.console.insert("end", txt, tag)
        else:
            self.console.insert("end", txt)
        self.console.see("end")
        self.console.configure(state="disabled")

    def _poll(self):
        while True:
            try:
                self.q.get_nowait()()
            except queue.Empty:
                break
        self.after(200, self._poll)

    def run_bg(self, fn, on_done):
        def work():
            try:
                res, err = fn(), None
            except Exception as e:
                res, err = None, e
            self.q.put(lambda: on_done(res, err))
        threading.Thread(target=work, daemon=True).start()

    # --- UI ---
    def _build(self):
        pad = {"padx": 10, "pady": 6}

        hd = tk.Frame(self, bg=self.BG)
        hd.pack(fill="x", padx=10, pady=(8, 2))
        self._logo = self._carregar_logo()
        if self._logo:
            tk.Label(hd, image=self._logo, bg=self.BG, bd=0).pack(side="left", padx=(0, 14))
        tx = tk.Frame(hd, bg=self.BG)
        tx.pack(side="left", anchor="w")
        tk.Label(tx, text="DeFiAnts", font=("Consolas", 26, "bold"), fg=self.ROXO, bg=self.BG).pack(anchor="w")
        tk.Label(tx, text="AI TRADER (GEMINI)  ·  cbBTC / WETH  ·  BASE", font=("Consolas", 11, "bold"),
                 fg=self.CIANO, bg=self.BG).pack(anchor="w")
        tk.Label(tx, text="BUILD · HODL · EARN · REPEAT", font=("Consolas", 9), fg=self.SUAVE, bg=self.BG).pack(anchor="w")
        self.lbl_modo = tk.Label(hd, text="", font=("Consolas", 12, "bold"), bg=self.BG)
        self.lbl_modo.pack(side="right", padx=8)

        w = ttk.LabelFrame(self, text="Carteira (REAL, rede Base). Usa uma carteira dedicada ao bot.")
        w.pack(fill="x", **pad)
        ttk.Label(w, text="Chave privada:").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        self.pk = ttk.Entry(w, show="*", width=58)
        self.pk.grid(row=0, column=1, padx=6, pady=3)
        ttk.Label(w, text="Palavra-passe:").grid(row=1, column=0, sticky="w", padx=6, pady=3)
        self.pw = ttk.Entry(w, show="*", width=58)
        self.pw.grid(row=1, column=1, padx=6, pady=3)
        bt = ttk.Frame(w)
        bt.grid(row=2, column=0, columnspan=2, pady=4)
        ttk.Button(bt, text="Importar chave (cifra se houver palavra-passe)", command=self.on_importar).pack(side="left", padx=4)
        ttk.Button(bt, text="Desbloquear wallet.json", command=self.on_desbloquear).pack(side="left", padx=4)
        self.lbl_addr = ttk.Label(w, text="Carteira: (nenhuma)")
        self.lbl_addr.grid(row=3, column=0, columnspan=2, sticky="w", padx=6, pady=3)

        s = ttk.LabelFrame(self, text="Saldos")
        s.pack(fill="x", **pad)
        self.lbl_saldos = ttk.Label(s, text="ETH: -    WETH: -    cbBTC: -")
        self.lbl_saldos.pack(side="left", padx=8, pady=6)
        ttk.Button(s, text="Atualizar", command=self.on_saldos).pack(side="right", padx=8)

        e = ttk.LabelFrame(self, text="Enviar todo o saldo (ETH + WETH + cbBTC)")
        e.pack(fill="x", **pad)
        ttk.Label(e, text="Endereço de destino:").grid(row=0, column=0, padx=6, pady=4)
        self.dest = ttk.Entry(e, width=56)
        self.dest.grid(row=0, column=1, padx=6, pady=4)
        tk.Button(e, text="ENVIAR TODO O SALDO", bg=self.VERMELHO, fg="white",
                  font=("TkDefaultFont", 10, "bold"), command=self.on_enviar).grid(row=1, column=0, columnspan=2, pady=6)

        # --- chave do Gemini ---
        g = ttk.LabelFrame(self, text="Gemini (API key)")
        g.pack(fill="x", **pad)
        ttk.Label(g, text="Chave:").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        self.gkey = ttk.Entry(g, show="*", width=52)
        self.gkey.grid(row=0, column=1, padx=6, pady=3)
        self.var_gsave = tk.BooleanVar(value=True)
        ttk.Checkbutton(g, text="Guardar em gemini.key", variable=self.var_gsave).grid(row=0, column=2, padx=6)
        gb = ttk.Frame(g)
        gb.grid(row=1, column=0, columnspan=3, sticky="w", padx=6, pady=3)
        tk.Button(gb, text="Aplicar chave", bg=self.ROXO, fg="white", command=self.on_gemini_aplicar).pack(side="left", padx=4)
        ttk.Button(gb, text="Testar", command=self.on_gemini_testar).pack(side="left", padx=4)
        ttk.Button(gb, text="Apagar guardada", command=self.on_gemini_apagar).pack(side="left", padx=4)
        self.lbl_gkey = ttk.Label(gb, text="")
        self.lbl_gkey.pack(side="left", padx=10)
        self._gemini_estado()

        # --- parâmetros editáveis ---
        pf = ttk.LabelFrame(self, text="Limites de proteção (o Gemini decide o resto; lidos a cada ciclo)")
        pf.pack(fill="x", **pad)
        self.ents = {}
        for i, (k, label, _d, _lo, _hi, _t) in enumerate(FIELDS):
            r, c = divmod(i, 3)
            ttk.Label(pf, text=label).grid(row=r, column=c * 2, sticky="w", padx=(8, 4), pady=3)
            ent = ttk.Entry(pf, width=9)
            ent.grid(row=r, column=c * 2 + 1, padx=(0, 10), pady=3)
            self.ents[k] = ent
        self._fill_ents()
        pb = ttk.Frame(pf)
        pb.grid(row=(len(FIELDS) - 1) // 3 + 1, column=0, columnspan=6, pady=4)
        tk.Button(pb, text="Aplicar e guardar", bg=self.ROXO, fg="white",
                  command=self.on_aplicar).pack(side="left", padx=6)
        ttk.Button(pb, text="Repor padrões", command=self.on_repor).pack(side="left", padx=6)
        ttk.Label(pf, foreground=self.SUAVE,
                  text="O Gemini decide quando entrar/sair e quanto usar. Capital e modo só mudam com o bot parado e sem posição."
                  ).grid(row=(len(FIELDS) - 1) // 3 + 2, column=0, columnspan=6, sticky="w", padx=8, pady=(0, 4))

        t = ttk.LabelFrame(self, text="Bot de scalp cbBTC/WETH")
        t.pack(fill="x", **pad)
        self.var_live = tk.BooleanVar(value=LIVE_TRADING)
        ttk.Checkbutton(t, text="Modo REAL (usa fundos da carteira; desmarcado = simulação)",
                        variable=self.var_live).pack(side="top", anchor="w", padx=8, pady=(4, 0))
        row = ttk.Frame(t)
        row.pack(fill="x")
        tk.Button(row, text="Iniciar", bg=self.VERDE, fg="white", width=10,
                  command=self.on_iniciar).pack(side="left", padx=8, pady=6)
        tk.Button(row, text="Parar", bg=self.VERMELHO, fg="white", width=10,
                  command=self.bot.parar).pack(side="left", padx=4)
        tk.Button(row, text="Fechar posição", bg=self.LARANJA, fg="white",
                  command=self.bot.fechar_manual).pack(side="left", padx=8)
        self.lbl_bot = ttk.Label(t, text="")
        self.lbl_bot.pack(side="top", anchor="w", padx=10, pady=(0, 6))
        self._fg_default = str(self.lbl_bot.cget("foreground"))

        self.console = scrolledtext.ScrolledText(
            self, height=14, state="disabled", wrap="word", bg="#07031a", fg=self.TEXTO, insertbackground=self.TEXTO,
            relief="flat", font=("Consolas", 10))
        self.console.pack(fill="both", expand=True, **pad)
        self.console.tag_config("err", foreground=self.TXT_ERR)
        self.console.tag_config("ok", foreground=self.TXT_OK)
        self.console.tag_config("live", foreground=self.TXT_LIVE)
        self.console.tag_config("think", foreground=self.CIANO)
        tk.Label(self, text="COLONY OVER EVERYTHING  ·  DF-ANTS-001", font=("Consolas", 8),
                 fg=self.SUAVE, bg=self.BG).pack(pady=(0, 6))

    # --- Gemini ---
    def _gemini_estado(self):
        self.lbl_gkey.config(text=f"Chave: {origem_chave_gemini()}")

    def on_gemini_aplicar(self):
        guardar = self.var_gsave.get()
        try:
            definir_chave_gemini(self.gkey.get(), guardar)
        except ValueError as err:
            messagebox.showerror("Gemini", str(err))
            return
        except OSError as err:
            self.log(f"ERRO a guardar gemini.key (a chave fica só nesta sessão): {err}")
        else:
            self.log("Chave do Gemini aplicada" + (" e guardada em gemini.key." if guardar else " (só nesta sessão)."))
        self.gkey.delete(0, "end")
        self.bot.gemini_ate = 0.0  # volta a tentar o Gemini já no próximo ciclo
        self._gemini_estado()

    def on_gemini_testar(self):
        if not obter_chave_gemini():
            messagebox.showerror("Gemini", "Ainda não há chave definida.")
            return
        self.log("A testar o Gemini...")

        def done(res, err):
            self.log(f"ERRO no teste do Gemini: {err}" if err else f"Gemini a responder: {res}")
        self.run_bg(testar_gemini, done)

    def on_gemini_apagar(self):
        apagar_chave_gemini()
        self.log("Chave do Gemini removida (sessão e gemini.key).")
        self._gemini_estado()

    # --- parâmetros ---
    def _fill_ents(self):
        for k, ent in self.ents.items():
            ent.delete(0, "end")
            ent.insert(0, f"{cfg.v[k]:g}")

    def _aplicar(self) -> bool:
        try:
            cfg.update({k: ent.get() for k, ent in self.ents.items()})
        except ValueError as err:
            messagebox.showerror("Parâmetro inválido", str(err))
            return False
        return True

    def on_aplicar(self):
        if self._aplicar():
            self.bot.sincronizar_ajustes()
            self.log("Parâmetros aplicados e guardados em settings.json.")
            if abs(cfg.capital - self.bot.capital) > 1e-12:
                self.log("O novo capital só entra em vigor no próximo Iniciar (sem posição aberta).")

    def on_repor(self):
        cfg.reset()
        self._fill_ents()
        self.log("Parâmetros repostos para os valores do .env.")

    # --- ações ---
    def _set_wallet(self, addr):
        self.lbl_addr.config(text=f"Carteira: {addr}")
        self.log(f"Carteira carregada: {addr}")
        self.on_saldos()

    def on_importar(self):
        try:
            addr = self.wallet.importar(self.pk.get(), self.pw.get() or None)
        except Exception:
            messagebox.showerror("Erro", "Chave privada inválida.")
            return
        self.pk.delete(0, "end")
        if self.pw.get():
            self.log("Chave guardada cifrada em wallet.json.")
        else:
            self.log("Chave só em memória (será perdida ao fechar).")
        self._set_wallet(addr)

    def on_desbloquear(self):
        if not os.path.exists(KEYSTORE):
            messagebox.showerror("Erro", "wallet.json não existe. Importa a chave primeiro.")
            return
        try:
            addr = self.wallet.desbloquear(self.pw.get())
        except Exception:
            messagebox.showerror("Erro", "Palavra-passe errada.")
            return
        self._set_wallet(addr)

    def on_iniciar(self):
        if not self._aplicar():  # aplica o que está nos campos antes de arrancar
            return
        live = self.var_live.get()
        if live:
            if not self.wallet.account:
                messagebox.showerror("Erro", "Importa ou desbloqueia uma carteira primeiro.")
                return
            if not messagebox.askyesno(
                "Trading REAL",
                f"MODO REAL: o bot vai fazer swaps com fundos de\n{self.wallet.account.address}\n\n"
                f"Capital: {cfg.capital:g} WETH, {cfg.order_pct:g}% por ordem\n"
                f"O GEMINI decide entradas, saídas e tamanho (sem TP/SL fixos)\n"
                f"Perda total máx. {cfg.max_loss_pct:g}% | Slippage máx. {cfg.slippage_pct:g}%\n\n"
                "Confirmas?",
            ):
                return
        self.bot.iniciar(live)

    def on_saldos(self):
        if not self.wallet.account:
            return

        def done(res, err):
            if err:
                self.log(f"Erro a ler saldos: {err}")
                return
            self.ultimos_saldos = res
            self.lbl_saldos.config(
                text=f"ETH: {res['ETH']:.6f}    WETH: {res['WETH']:.6f}    cbBTC: {res['cbBTC']:.8f}")
        self.run_bg(self.wallet.saldos, done)

    def on_enviar(self):
        if not self.wallet.account:
            messagebox.showerror("Erro", "Importa ou desbloqueia uma carteira primeiro.")
            return
        dest = self.dest.get().strip()
        if not Web3.is_address(dest):
            messagebox.showerror("Erro", "Endereço de destino inválido.")
            return
        dest = Web3.to_checksum_address(dest)
        if not messagebox.askyesno(
            "Confirmar envio",
            f"Vais enviar TODO o saldo (ETH, WETH e cbBTC) de\n{self.wallet.account.address}\n\npara\n{dest}\n\n"
            "Esta ação é irreversível. Confirmas?",
        ):
            return
        self.log(f"A enviar todo o saldo para {dest}...")

        def done(res, err):
            if err:
                self.log(f"ERRO no envio: {err}")
                messagebox.showerror("Erro", str(err))
            else:
                for m in res:
                    self.log(m)
            self.on_saldos()
        self.run_bg(lambda: self.wallet.enviar_tudo(dest), done)

    def _refresh_status(self):
        b = self.bot
        pos = "nenhuma"
        if b.posicao:
            pos = "aberta" + (f" ({b.var_atual * 100:+.2f}%)" if b.var_atual is not None else "")
        lucro = b.saldo - b.capital
        pct = lucro / b.capital * 100 if b.capital else 0.0
        win = f"{b.vitorias / b.trades * 100:.0f}%" if b.trades else "-"
        cor = self.TXT_OK if lucro > 0 else self.TXT_ERR if lucro < 0 else self._fg_default
        live = b.live if b.running else self.var_live.get()
        self.lbl_modo.config(text="● MODO REAL" if live else "● SIMULAÇÃO",
                             fg=self.TXT_ERR if live else self.CIANO)
        self.lbl_bot.config(
            text=f"{'A correr' if b.running else 'Parado'} [{'REAL' if b.live else 'SIM'}] | saldo {b.saldo:.6f} WETH | "
                 f"lucro {lucro:+.6f} ({pct:+.2f}%) | posição {pos} | trades {b.trades} (acerto {win})\n"
                 f"Gemini ajustou: confiança mín. {b.min_conf_atual:.2f} | cooldown {b.cooldown_atual}s | "
                 f"ciclo {b.loop_atual}s | modelo {b.modelo_atual or '-'}",
            foreground=cor)
        if b.running and b.live and b.carteira:   # saldos reais que o bot (e o Gemini) estão a ver
            c = b.carteira
            self.lbl_saldos.config(text=f"ETH: {c['ETH']:.6f}    WETH: {c['WETH']:.6f}    cbBTC: {c['cbBTC']:.8f}")
        self.after(2000, self._refresh_status)


if __name__ == "__main__":
    App().mainloop()
