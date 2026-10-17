# DeFiAnts · AI Trader cbBTC/WETH (Base)

Aplicação local (Tkinter) em que o **Gemini gere uma carteira on-chain** no par cbBTC/WETH, via Uniswap V3 na rede Base.

> BUILD · HODL · EARN · REPEAT

## Aviso importante

Software experimental, sem qualquer garantia. Em **modo REAL** usa fundos reais e podes perder tudo.
Usa **sempre uma carteira dedicada** só com o que estás disposto a perder. Não é aconselhamento financeiro.
Começa sempre em **simulação**.

## Funcionalidades

- Carteira real: importar chave privada (cifrada em `wallet.json`), ver saldos, enviar todo o saldo.
- Modo simulação ou REAL (swaps no Uniswap V3 / Base).
- O Gemini decide quando comprar, vender e quanto arriscar (sem TP/SL fixos).
- Limites de proteção da conta: slippage máx., perda total máx. (kill switch), cooldown, confiança mínima, tamanho máx. por ordem.
- Rotação automática entre modelos Gemini/Gemma quando a quota de um esgota.
- Registo de trades em `trades.csv` e retoma da posição REAL após reiniciar (`estado.json`).

## Instalação

```bash
git clone <URL-DO-TEU-REPO>
cd <pasta>
python -m venv .venv
# Windows: .venv\Scripts\activate   |   Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # Windows: copy .env.example .env
python scalp_bot_local.py
```

1. Edita o `.env` (RPC da Base e, opcionalmente, a chave do Gemini).
2. Chave gratuita do Gemini: [Google AI Studio](https://aistudio.google.com/apikey).
3. Opcional: coloca um `falcon.png` na pasta para aparecer o logo na janela.

## Ficheiros locais (não vão para o Git)

`.env`, `wallet.json`, `gemini.key`, `settings.json`, `estado.json`, `trades.csv` estão no `.gitignore`.
**Nunca os publiques.**

## Antes de usar fundos reais

Confirma os endereços dos contratos Uniswap V3 na Base (SwapRouter02, QuoterV2, Factory) na documentação oficial da Uniswap.
