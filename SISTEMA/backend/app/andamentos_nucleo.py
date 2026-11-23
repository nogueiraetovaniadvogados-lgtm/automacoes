"""Linha do tempo do processo (28/09/2026, migracao do ADVBOX).

`jur_andamentos` junta, lado a lado, o que a equipe escreve no sistema e a copia
(so LIDA) dos andamentos externos:
  - origem 'advbox': copiado do /history do ADVBOX ate 19/11/2026 (ele saiu);
  - origem 'infinitum': banco Movimentacoes do Infinitum (INTEGRACOES/infinitum_dados).

A chave de cada item e o hash de processo + criado_em + tipo + texto: rodar de
novo nao duplica.

Quem usa:
  - routers/andamentos.py (tela: ficha do processo e ficha da tarefa);
  - audiencias_vigia.py (checklist da Dra. Advogada: juntadas, testemunhas,
    pedido de audiencia virtual) le daqui.
"""
import hashlib
import os
import sqlite3
import time
from pathlib import Path

DB = str(Path(__file__).resolve().parents[1] / 'sistema_esc.db')


def con(db=None):
    c = sqlite3.connect(db or DB, timeout=30)
    c.row_factory = sqlite3.Row
    garantir(c)
    return c


def garantir(c):
    c.execute("""CREATE TABLE IF NOT EXISTS jur_andamentos (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   processo_id INTEGER, processo_numero TEXT, lawsuit_id INTEGER,
                   tipo TEXT, texto TEXT, autor TEXT, autor_id INTEGER, responsavel TEXT,
                   data_tarefa TEXT, prazo TEXT, criado_em TEXT, origem TEXT,
                   chave TEXT UNIQUE, editado_em TEXT, removido INTEGER DEFAULT 0)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_andam_proc ON jur_andamentos (processo_id, criado_em)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_andam_num ON jur_andamentos (processo_numero)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_andam_lid ON jur_andamentos (lawsuit_id)")
    c.execute("""CREATE TABLE IF NOT EXISTS jur_andamentos_sync (
                   processo_id INTEGER PRIMARY KEY, lawsuit_id INTEGER,
                   sincronizado_em TEXT, itens INTEGER)""")


def agora_txt():
    return time.strftime('%Y-%m-%d %H:%M:%S')


ORIGENS_EXTERNAS = ('advbox', 'infinitum')   # copias so de leitura (nao se editam na tela)


def _infinitum():
    """INTEGRACOES/infinitum_dados (o ADVBOX saiu em 19/11/2026). None se indisponivel."""
    import sys
    integ = str(Path(__file__).resolve().parents[3] / 'INTEGRACOES')
    if integ not in sys.path:
        sys.path.insert(0, integ)
    try:
        import infinitum_dados as INFD
    except Exception:
        return None
    return INFD if INFD.ativo() else None


def historico_externo(lawsuit_id=None, numero=None):
    """Andamentos do processo no banco Movimentacoes do Infinitum, no formato do /history do
    ADVBOX (created_at, comments, task, author...). None quando nao deu para ler."""
    INFD = _infinitum()
    if INFD is None or not (lawsuit_id or numero):
        return None
    try:
        return INFD.listar_historico(lawsuit_id, numero)
    except Exception:
        return None


def chave(lawsuit_id, criado_em, tipo, texto, prefixo='adv'):
    """processo + criado_em + tipo + texto: rodar de novo nao duplica. 'adv:' sao os itens
    copiados do ADVBOX ate 19/11/2026; 'inf:' os do Infinitum."""
    base = '%s|%s|%s|%s' % (lawsuit_id, str(criado_em or '')[:19], (tipo or '').strip(), (texto or '').strip())
    return prefixo + ':' + hashlib.sha1(base.encode('utf-8')).hexdigest()[:24]


def gravar_item(c, processo_id, numero, lawsuit_id, tipo, texto, autor, responsavel,
                data_tarefa, prazo, criado_em, origem='infinitum'):
    """Grava um item externo. Se ja existe, so completa autor/responsavel que faltavam.
    Devolve 1 se era novo."""
    k = chave(lawsuit_id or numero, criado_em, tipo, texto, 'adv' if origem == 'advbox' else 'inf')
    cur = c.execute(
        'INSERT OR IGNORE INTO jur_andamentos (processo_id, processo_numero, lawsuit_id, tipo, texto, '
        'autor, responsavel, data_tarefa, prazo, criado_em, origem, chave) '
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (processo_id, numero or None, lawsuit_id, (tipo or '').strip() or None, (texto or '').strip() or None,
         (autor or '').strip() or None, (responsavel or '').strip() or None,
         str(data_tarefa or '')[:10] or None, str(prazo or '')[:10] or None,
         str(criado_em or '')[:19] or None, origem, k))
    if cur.rowcount:
        return 1
    if autor or responsavel:
        c.execute("UPDATE jur_andamentos SET autor=COALESCE(NULLIF(autor,''), ?), "
                  "responsavel=COALESCE(NULLIF(responsavel,''), ?) WHERE chave=?",
                  ((autor or '').strip() or None, (responsavel or '').strip() or None, k))
    return 0


def gravar_externo(c, processo_id, numero, lawsuit_id, historico):
    """Grava os andamentos do Infinitum (formato /history). Devolve os novos."""
    return sum(gravar_item(c, processo_id, numero or x.get('process_number'), lawsuit_id, x.get('task'),
                           x.get('comments'), x.get('author'), x.get('responsible'), x.get('start'),
                           x.get('date_deadline'), x.get('created_at'))
               for x in historico or [])


def sincronizar(c, processo_id, lawsuit_id, numero, paciencia=False):
    """Le os andamentos do processo no Infinitum e grava. Devolve (novos, lidos) ou None se
    o Infinitum nao respondeu. `paciencia` ficou do ADVBOX (rate limit) e e ignorado."""
    h = historico_externo(lawsuit_id, numero)
    if h is None:
        return None
    novos = gravar_externo(c, processo_id, numero, lawsuit_id, h)
    c.execute('INSERT OR REPLACE INTO jur_andamentos_sync (processo_id, lawsuit_id, sincronizado_em, itens) '
              'VALUES (?,?,?,?)', (processo_id, lawsuit_id, agora_txt(), len(h)))
    c.commit()
    return novos, len(h)


def historico_local(lawsuit_id=None, numero=None, db=None):
    """No formato do /history do ADVBOX (created_at, comments, task, author), do
    mais novo para o mais antigo. Lista vazia quando nao ha nada guardado."""
    if not (lawsuit_id or numero):
        return []
    c = con(db)
    try:
        linhas = c.execute(
            "SELECT criado_em, texto, tipo, autor FROM jur_andamentos WHERE removido=0 "
            "AND (lawsuit_id=? OR processo_numero=?) ORDER BY criado_em DESC",
            (lawsuit_id or -1, numero or '#')).fetchall()
    finally:
        c.close()
    return [{'created_at': r['criado_em'], 'comments': r['texto'] or '', 'task': r['tipo'],
             'author': r['autor']} for r in linhas]
