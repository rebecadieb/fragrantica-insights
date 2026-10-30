import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))
EXEMPLO = RAIZ / "data" / "exemplo"


def lote(*mencoes: tuple) -> dict:
    """Saída do estágio 1. Ex.: lote((1, 1, "fixacao", "+"))."""
    return {"mencoes": [{"r": r, "f": f, "d": d, "p": p} for r, f, d, p in mencoes], "perfis": [], "ocasioes": []}
