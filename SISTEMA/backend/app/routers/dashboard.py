""
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import usuario_atual
from ..database import get_db, Usuario
from ..whatsapp.models import Conversa, Fluxo, Pipeline

router = APIRouter(prefix='/api/dashboard', tags=['dashboard'])


@router.get('/resumo')
async def resumo(user: Usuario = Depends(usuario_atual), db: AsyncSession = Depends(get_db)):
    agora = datetime.utcnow()
    inicio_hoje = agora.replace(hour=0, minute=0, second=0, microsecond=0)
    inicio_semana = inicio_hoje - timedelta(days=6)
    inicio_serie = inicio_hoje - timedelta(days=13)

    async def _count(where=None):
        q = select(func.count(Conversa.id))
        if where is not None:
            q = q.where(where)
        return (await db.execute(q)).scalar() or 0

    total = await _count()
    nao_lidas = await _count(Conversa.nao_lidas > 0)
    novos_hoje = await _count(Conversa.criado_em >= inicio_hoje)
    novos_semana = await _count(Conversa.criado_em >= inicio_semana)

    tag_rows = (await db.execute(
        select(Conversa.tag, func.count(Conversa.id)).group_by(Conversa.tag))).all()
    por_tag = {}
    for t, c in tag_rows:
        t = (t or '').strip()
        if t:
            por_tag[t] = c

    fx = (await db.execute(
        select(Fluxo.tipo, func.count(Fluxo.id)).where(Fluxo.ativo == True).group_by(Fluxo.tipo))).all()
    fluxos_ativos = {t: c for t, c in fx}

    pipe = (await db.execute(select(Pipeline.id).where(Pipeline.nome == 'Produção'))).scalar_one_or_none()
    em_producao, producao_fases = 0, {}
    if pipe:
        em_producao = await _count(Conversa.pipeline_id == pipe)
        pf = (await db.execute(
            select(Conversa.etapa, func.count(Conversa.id))
            .where(Conversa.pipeline_id == pipe).group_by(Conversa.etapa))).all()
        producao_fases = {e: c for e, c in pf if e}

    serie_rows = (await db.execute(
        select(func.date(Conversa.criado_em), func.count(Conversa.id))
        .where(Conversa.criado_em >= inicio_serie)
        .group_by(func.date(Conversa.criado_em)))).all()
    mapa = {str(d): c for d, c in serie_rows}
    serie = []
    for i in range(14):
        dia = (inicio_serie + timedelta(days=i)).date()
        serie.append({'dia': dia.isoformat(), 'valor': mapa.get(dia.isoformat(), 0)})

    return {
        'conversas_total': total,
        'nao_lidas': nao_lidas,
        'novos_hoje': novos_hoje,
        'novos_semana': novos_semana,
        'fluxos_ativos': fluxos_ativos,
        'fluxos_total': sum(fluxos_ativos.values()),
        'em_producao': em_producao,
        'producao_fases': producao_fases,
        'por_tag': por_tag,
        'serie_novos': serie,
    }


# ---------------------------------------------------------------------------
# PAINEL DE METAS NA TV (pedido do kickoff de 09/11/2026)
# Leitura pura, sem login interativo: a TV abre /painel-metas.html?token=...
# e esta rota so responde se o token bater com PAINEL_METAS_TOKEN do .env.
# Tudo que depende de tabela inexistente devolve 0 e entra em "fonte_pendente".
# ---------------------------------------------------------------------------
import hmac
import os
import re
import sqlite3
from pathlib import Path

from fastapi import HTTPException, Query

_DB_PAINEL = Path(__file__).resolve().parents[2] / 'sistema_esc.db'


def _painel_db() -> str:
    url = os.getenv('DATABASE_URL', '')
    if url.startswith('sqlite') and ':///' in url:
        p = Path(url.split(':///', 1)[1])
        if not p.is_absolute():
            p = Path(__file__).resolve().parents[2] / p
        return str(p)
    return str(_DB_PAINEL)


def _int_env(nome, padrao):
    try:
        return int(float((os.getenv(nome) or str(padrao)).replace(',', '.')))
    except ValueError:
        return padrao


def _float_env(nome, padrao=0.0):
    try:
        return float((os.getenv(nome) or str(padrao)).replace(',', '.'))
    except ValueError:
        return padrao


def _tem_tabela(c, nome):
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (nome,)).fetchone())


def _colunas(c, nome):
    return {r[1] for r in c.execute(f'PRAGMA table_info({nome})')}


def _montar_painel(mes: str) -> dict:
    ini = mes + '-01'
    a, m = int(mes[:4]), int(mes[5:7])
    fim = '%04d-%02d-01' % (a + (m // 12), m % 12 + 1)
    rx_prot = re.compile(os.getenv('PAINEL_ETAPA_PROTOCOLO', 'DISTRIBU|PROTOCOL'), re.I)
    rx_cont = re.compile(os.getenv('PAINEL_ETAPA_CONTRATO', 'ASSINAD|FECHOU|FECHADO|CONTRATO'), re.I)
    meta_prot = _int_env('PAINEL_META_PROTOCOLOS', 30)
    meta_casos = _int_env('PAINEL_META_CASOS_NOVOS', 100)
    bonus_contrato = _float_env('PAINEL_BONUS_POR_CONTRATO', 0.0)
    bonus_protocolo = _float_env('PAINEL_BONUS_POR_PROTOCOLO', 0.0)
    pendente = []
    saida = {'mes': mes, 'gerado_em': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
             'metas': {'protocolos': meta_prot, 'casos_novos': meta_casos}}
    path = _painel_db()
    if not Path(path).exists():
        pendente.append('banco')
        saida.update({'protocolos': 0, 'contratos': 0, 'casos_novos': 0, 'leads': 0, 'conversao_pct': 0.0,
                      'leads_por_canal': [], 'ranking': [], 'processos_cadastrados': 0,
                      'bonificacao_total': 0.0, 'fonte_pendente': pendente})
        return saida
    c = sqlite3.connect(path, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        nomes = {}
        if _tem_tabela(c, 'usuarios'):
            for r in c.execute('SELECT email, nome FROM usuarios'):
                if r['email']:
                    nomes[r['email'].lower()] = r['nome'] or r['email']

        prot_ids, cont_por_conv = set(), {}
        tem_quadro = _tem_tabela(c, 'wa_board_hist')
        if tem_quadro:
            for r in c.execute("SELECT conversa_id, para_etapa, quem FROM wa_board_hist "
                               "WHERE bloqueado=0 AND criado_em >= ? AND criado_em < ? ORDER BY id", (ini, fim)):
                etapa = r['para_etapa'] or ''
                if rx_prot.search(etapa):
                    prot_ids.add(r['conversa_id'])
                elif rx_cont.search(etapa) and r['conversa_id'] not in cont_por_conv:
                    q = (r['quem'] or '').lower()
                    cont_por_conv[r['conversa_id']] = q if '@' in q and not q.startswith('auto:') else ''
        else:
            pendente.append('wa_board_hist')

        leads, canais, dono_conv = 0, {}, {}
        if _tem_tabela(c, 'wa_conversas'):
            cols = _colunas(c, 'wa_conversas')
            for r in c.execute("SELECT origem, COUNT(*) n FROM wa_conversas WHERE criado_em >= ? AND criado_em < ? "
                               "GROUP BY origem", (ini, fim)):
                k = (r['origem'] or '').strip() or 'WhatsApp direto'
                canais[k] = canais.get(k, 0) + r['n']
                leads += r['n']
            if cont_por_conv and 'atendente_email' in cols:
                ids = list(cont_por_conv)
                for i in range(0, len(ids), 500):
                    lote = ids[i:i + 500]
                    for r in c.execute('SELECT id, atendente_email FROM wa_conversas WHERE id IN (%s)'
                                       % ','.join('?' * len(lote)), lote):
                        dono_conv[r['id']] = (r['atendente_email'] or '').lower()
        else:
            pendente.append('wa_conversas')

        processos_mes = 0
        if _tem_tabela(c, 'jur_processos'):
            for r in c.execute('SELECT data_processo, criado_em FROM jur_processos'):
                d = (r['data_processo'] or '').strip()
                if re.fullmatch(r'\d{2}/\d{2}/\d{4}', d):
                    d = d[6:10] + '-' + d[3:5] + '-' + d[0:2]
                d = d or str(r['criado_em'] or '')[:10]
                if ini <= d[:10] < fim:
                    processos_mes += 1
        else:
            pendente.append('jur_processos')

        # protocolo = card que andou para distribuicao/protocolo no quadro; sem quadro, processos do mes
        protocolos = len(prot_ids) if tem_quadro else processos_mes
        contratos = len(cont_por_conv)

        rank = {}
        for cid, quem in cont_por_conv.items():
            k = quem or dono_conv.get(cid) or 'automacao'
            rank.setdefault(k, {'contratos': 0, 'moedas': 0})
            rank[k]['contratos'] += 1
        if _tem_tabela(c, 'slcoin_extrato') and _tem_tabela(c, 'rh_colaboradores'):
            for r in c.execute("SELECT lower(co.email) email, SUM(e.quantidade) q FROM slcoin_extrato e "
                               "JOIN rh_colaboradores co ON co.id = e.colaborador_id "
                               "WHERE e.tipo='ganho' AND e.data_credito >= ? AND e.data_credito < ? "
                               "GROUP BY co.email", (ini, fim)):
                if r['email']:
                    rank.setdefault(r['email'], {'contratos': 0, 'moedas': 0})
                    rank[r['email']]['moedas'] = int(r['q'] or 0)
        else:
            pendente.append('slcoin_extrato')
        ranking = []
        for email, v in rank.items():
            nome = 'Automação' if email == 'automacao' else nomes.get(email, email.split('@')[0].title())
            ranking.append({'nome': nome, 'contratos': v['contratos'], 'moedas': v['moedas'],
                            'bonus': round(v['contratos'] * bonus_contrato, 2)})
        ranking.sort(key=lambda x: (-x['contratos'], -x['moedas'], x['nome']))

        saida.update({
            'protocolos': protocolos,
            'contratos': contratos,
            'casos_novos': contratos,
            'leads': leads,
            'conversao_pct': round(100.0 * contratos / leads, 1) if leads else 0.0,
            'leads_por_canal': sorted(({'canal': k, 'leads': v} for k, v in canais.items()),
                                      key=lambda x: -x['leads']),
            'ranking': ranking[:10],
            'processos_cadastrados': processos_mes,
            'bonificacao_total': round(contratos * bonus_contrato + protocolos * bonus_protocolo, 2),
            'fonte_pendente': pendente,
        })
        return saida
    finally:
        c.close()


@router.get('/painel-metas')
def painel_metas(token: str = Query(''), mes: str = Query('')):
    """Numeros do painel da TV. So leitura; protegido pelo token do .env."""
    esperado = os.getenv('PAINEL_METAS_TOKEN', '')
    if not esperado:
        raise HTTPException(503, 'Painel desligado: defina PAINEL_METAS_TOKEN no .env')
    if not hmac.compare_digest(token or '', esperado):
        raise HTTPException(401, 'Token invalido')
    if not re.fullmatch(r'\d{4}-\d{2}', mes or ''):
        mes = datetime.now().strftime('%Y-%m')
    return _montar_painel(mes)


# ---------------------------------------------------------------------------
# CENTRAL DE COMANDO (/central.html, 13/11/2026)
# Junta numa chamada o que nao tem resumo pronto em outro modulo: funil do CRM,
# carteira por area/fase, clientes, casos, linha do tempo, telefonia, portal e
# fila de producao. Intimacoes (SYNC), campanhas (Meta), tarefas, audiencias,
# financeiro e RH a tela le das rotas de cada modulo, respeitando a permissao
# de cada um. Cada bloco roda isolado: tabela que nao existe vira bloco vazio
# e um aviso, nunca derruba a resposta.
# ---------------------------------------------------------------------------
import time as _time


def _bloco(nome, fn, avisos):
    try:
        return fn()
    except Exception as e:  # tabela faltando, coluna nova etc.
        avisos.append(f'{nome}: {str(e)[:120]}')
        return None


def _montar_central() -> dict:
    agora = datetime.now()
    hoje = agora.date()
    d7 = (hoje - timedelta(days=6)).isoformat()
    d30 = (hoje - timedelta(days=29)).isoformat()
    m12 = (hoje.replace(day=1) - timedelta(days=330)).replace(day=1).isoformat()
    avisos: list[str] = []
    saida = {'gerado_em': agora.isoformat(timespec='seconds'), 'avisos': avisos}
    path = _painel_db()
    if not Path(path).exists():
        avisos.append('banco nao encontrado')
        return saida
    c = sqlite3.connect(path, timeout=15)
    c.row_factory = sqlite3.Row

    def um(sql, args=()):
        r = c.execute(sql, args).fetchone()
        return (r[0] if r else 0) or 0

    def pares(sql, args=(), lim=None):
        out = [{'k': (r[0] if r[0] not in (None, '') else '(sem)'), 'v': r[1]} for r in c.execute(sql, args)]
        return out[:lim] if lim else out

    def serie_dias(sql, args, n=30):
        mapa = {str(r[0]): r[1] for r in c.execute(sql, args)}
        ini = hoje - timedelta(days=n - 1)
        dias = [(ini + timedelta(days=i)).isoformat() for i in range(n)]
        return [{'d': d, 'v': mapa.get(d, 0)} for d in dias]

    def meses():
        out, a, m = [], hoje.year, hoje.month
        for _ in range(12):
            out.append('%04d-%02d' % (a, m))
            m -= 1
            if m == 0:
                a, m = a - 1, 12
        return out[::-1]

    def serie_meses(sql, args):
        mapa = {str(r[0]): r[1] for r in c.execute(sql, args)}
        return [{'m': m, 'v': mapa.get(m, 0)} for m in meses()]

    try:
        def crm():
            cols = _colunas(c, 'wa_conversas')
            r = {
                'total': um('SELECT COUNT(*) FROM wa_conversas'),
                'abertas': um("SELECT COUNT(*) FROM wa_conversas WHERE COALESCE(status,'aberto')='aberto'"),
                'nao_lidas': um('SELECT COUNT(*) FROM wa_conversas WHERE nao_lidas>0'),
                'hoje': um('SELECT COUNT(*) FROM wa_conversas WHERE substr(criado_em,1,10)=?', (hoje.isoformat(),)),
                'sete': um('SELECT COUNT(*) FROM wa_conversas WHERE substr(criado_em,1,10)>=?', (d7,)),
                'trinta': um('SELECT COUNT(*) FROM wa_conversas WHERE substr(criado_em,1,10)>=?', (d30,)),
                'serie': serie_dias('SELECT substr(criado_em,1,10) d, COUNT(*) FROM wa_conversas '
                                    'WHERE substr(criado_em,1,10)>=? GROUP BY d', (d30,)),
                'por_origem': pares("SELECT COALESCE(NULLIF(TRIM(origem),''),'WhatsApp direto') o, COUNT(*) n "
                                    'FROM wa_conversas WHERE substr(criado_em,1,10)>=? GROUP BY o ORDER BY n DESC',
                                    (d30,), 8),
                'por_tag': pares("SELECT tag, COUNT(*) n FROM wa_conversas WHERE COALESCE(tag,'')<>'' "
                                 'GROUP BY tag ORDER BY n DESC', (), 8),
                'leads_mes': serie_meses('SELECT substr(criado_em,1,7) m, COUNT(*) FROM wa_conversas '
                                         'WHERE substr(criado_em,1,10)>=? GROUP BY m', (m12,)),
            }
            if 'produto' in cols:
                r['por_produto'] = pares("SELECT COALESCE(produto,'(sem)') p, COUNT(*) n FROM wa_conversas "
                                         'WHERE substr(criado_em,1,10)>=? GROUP BY p ORDER BY n DESC', (d30,), 8)
            if 'responsavel' in cols:
                r['por_responsavel'] = pares("SELECT responsavel, COUNT(*) n FROM wa_conversas "
                                             "WHERE COALESCE(responsavel,'')<>'' AND COALESCE(status,'aberto')='aberto' "
                                             'GROUP BY responsavel ORDER BY n DESC', (), 8)
            funis = []
            if _tem_tabela(c, 'wa_pipelines'):
                for p in c.execute('SELECT id, nome FROM wa_pipelines ORDER BY ordem, id').fetchall():
                    cont = {r[0]: r[1] for r in c.execute(
                        'SELECT etapa, COUNT(*) FROM wa_conversas WHERE pipeline_id=? GROUP BY etapa', (p['id'],))}
                    etapas = []
                    if _tem_tabela(c, 'wa_stages'):
                        for s in c.execute('SELECT nome, cor FROM wa_stages WHERE pipeline_id=? ORDER BY ordem, id',
                                           (p['id'],)).fetchall():
                            etapas.append({'nome': s['nome'], 'cor': s['cor'], 'n': cont.pop(s['nome'], 0)})
                    for k, v in cont.items():
                        etapas.append({'nome': k or '(sem etapa)', 'cor': '', 'n': v})
                    funis.append({'nome': p['nome'], 'total': sum(e['n'] for e in etapas), 'etapas': etapas})
            r['funis'] = funis
            if _tem_tabela(c, 'wa_board_hist'):
                rx = re.compile(os.getenv('PAINEL_ETAPA_CONTRATO', 'ASSINAD|FECHOU|FECHADO|CONTRATO'), re.I)
                fech = {}
                for h in c.execute('SELECT conversa_id, para_etapa, substr(criado_em,1,7) m FROM wa_board_hist '
                                   'WHERE bloqueado=0 AND substr(criado_em,1,10)>=? ORDER BY id', (m12,)):
                    if rx.search(h['para_etapa'] or '') and h['conversa_id'] not in fech:
                        fech[h['conversa_id']] = h['m']
                cont_m = {}
                for m in fech.values():
                    cont_m[m] = cont_m.get(m, 0) + 1
                r['contratos_mes'] = [{'m': m, 'v': cont_m.get(m, 0)} for m in meses()]
            return r
        saida['crm'] = _bloco('crm', crm, avisos)

        def processos():
            ativo = 'COALESCE(encerrado,0)=0'
            return {
                'total': um('SELECT COUNT(*) FROM jur_processos'),
                'ativos': um(f'SELECT COUNT(*) FROM jur_processos WHERE {ativo}'),
                'encerrados': um('SELECT COUNT(*) FROM jur_processos WHERE COALESCE(encerrado,0)=1'),
                'honorarios': round(um(f'SELECT SUM(valor_honorarios) FROM jur_processos WHERE {ativo}'), 2),
                'expectativa': round(um(f'SELECT SUM(valor_expectativa) FROM jur_processos WHERE {ativo}'), 2),
                'contingencia': round(um(f'SELECT SUM(contingencia) FROM jur_processos WHERE {ativo}'), 2),
                'por_area': pares(f"SELECT COALESCE(NULLIF(area,''),'(sem área)') a, COUNT(*) n FROM jur_processos "
                                  f'WHERE {ativo} GROUP BY a ORDER BY n DESC', (), 8),
                'por_fase': pares(f"SELECT COALESCE(NULLIF(fase,''),'(sem fase)') f, COUNT(*) n FROM jur_processos "
                                  f'WHERE {ativo} GROUP BY f ORDER BY n DESC', (), 12),
                'por_responsavel': pares(f"SELECT COALESCE(NULLIF(responsavel,''),'(sem)') r, COUNT(*) n "
                                         f'FROM jur_processos WHERE {ativo} GROUP BY r ORDER BY n DESC', (), 10),
                'novos_mes': serie_meses('SELECT substr(criado_em,1,7) m, COUNT(*) FROM jur_processos '
                                         'WHERE substr(criado_em,1,10)>=? GROUP BY m', (m12,)),
            }
        saida['processos'] = _bloco('processos', processos, avisos)

        def clientes():
            doc = "LENGTH(REPLACE(REPLACE(REPLACE(COALESCE(documento,''),'.',''),'-',''),'/',''))"
            return {
                'total': um('SELECT COUNT(*) FROM jur_clientes'),
                'novos_30d': um('SELECT COUNT(*) FROM jur_clientes WHERE substr(criado_em,1,10)>=?', (d30,)),
                'pf': um(f'SELECT COUNT(*) FROM jur_clientes WHERE {doc}=11'),
                'pj': um(f'SELECT COUNT(*) FROM jur_clientes WHERE {doc}=14'),
                'por_origem': pares("SELECT COALESCE(NULLIF(origem,''),'(sem)') o, COUNT(*) n FROM jur_clientes "
                                    'GROUP BY o ORDER BY n DESC', (), 8),
                'por_uf': pares("SELECT UPPER(COALESCE(NULLIF(TRIM(estado),''),'(sem)')) e, COUNT(*) n "
                                'FROM jur_clientes GROUP BY e ORDER BY n DESC', (), 8),
                'novos_mes': serie_meses('SELECT substr(criado_em,1,7) m, COUNT(*) FROM jur_clientes '
                                         'WHERE substr(criado_em,1,10)>=? GROUP BY m', (m12,)),
                'recentes': [dict(r) for r in c.execute(
                    'SELECT nome, cidade, estado, origem, substr(criado_em,1,10) criado FROM jur_clientes '
                    'ORDER BY criado_em DESC LIMIT 8')],
            }
        saida['clientes'] = _bloco('clientes', clientes, avisos)

        def casos():
            if not _tem_tabela(c, 'casos'):
                return None
            return {'total': um('SELECT COUNT(*) FROM casos'),
                    'por_status': pares('SELECT status, COUNT(*) n FROM casos GROUP BY status ORDER BY n DESC'),
                    'por_tipo': pares('SELECT tipo, COUNT(*) n FROM casos GROUP BY tipo ORDER BY n DESC', (), 8)}
        saida['casos'] = _bloco('casos', casos, avisos)

        def andamentos():
            if not _tem_tabela(c, 'jur_andamentos'):
                return None
            vivo = 'COALESCE(removido,0)=0'
            return {'sete': um(f'SELECT COUNT(*) FROM jur_andamentos WHERE {vivo} AND substr(criado_em,1,10)>=?', (d7,)),
                    'serie': serie_dias(f'SELECT substr(criado_em,1,10) d, COUNT(*) FROM jur_andamentos '
                                        f'WHERE {vivo} AND substr(criado_em,1,10)>=? GROUP BY d', (d30,)),
                    'recentes': [dict(r) for r in c.execute(
                        f'SELECT processo_numero, tipo, substr(texto,1,160) texto, autor, criado_em '
                        f'FROM jur_andamentos WHERE {vivo} ORDER BY criado_em DESC LIMIT 10')]}
        saida['andamentos'] = _bloco('andamentos', andamentos, avisos)

        def telefonia():
            if not _tem_tabela(c, 'tel_chamadas'):
                return None
            t0 = _time.mktime(hoje.timetuple())
            t7 = t0 - 6 * 86400
            atend = 'atendeu_em IS NOT NULL'
            return {'hoje': um('SELECT COUNT(*) FROM tel_chamadas WHERE criado_em>=?', (t0,)),
                    'sete': um('SELECT COUNT(*) FROM tel_chamadas WHERE criado_em>=?', (t7,)),
                    'atendidas_7d': um(f'SELECT COUNT(*) FROM tel_chamadas WHERE criado_em>=? AND {atend}', (t7,)),
                    'duracao_media': round(um(f'SELECT AVG(duracao) FROM tel_chamadas WHERE criado_em>=? AND {atend}',
                                              (t7,)) or 0),
                    'por_agente': pares("SELECT COALESCE(NULLIF(agente,''),usuario_email) a, COUNT(*) n "
                                        'FROM tel_chamadas WHERE criado_em>=? GROUP BY a ORDER BY n DESC', (t7,), 8),
                    'por_resultado': pares("SELECT COALESCE(NULLIF(resultado,''),status) r, COUNT(*) n "
                                           'FROM tel_chamadas WHERE criado_em>=? GROUP BY r ORDER BY n DESC', (t7,), 8)}
        saida['telefonia'] = _bloco('telefonia', telefonia, avisos)

        def portal():
            if not _tem_tabela(c, 'portal_acesso'):
                return None
            t30 = _time.time() - 30 * 86400
            r = {'acessos': um('SELECT COUNT(*) FROM portal_acesso WHERE COALESCE(ativo,1)=1'),
                 'login_30d': um('SELECT COUNT(*) FROM portal_acesso WHERE ultimo_login>=?', (t30,)),
                 'logins_total': um('SELECT SUM(logins) FROM portal_acesso')}
            if _tem_tabela(c, 'portal_nps'):
                n = c.execute('SELECT COUNT(*), AVG(facil), AVG(gostou), AVG(faz_sentido) FROM portal_nps '
                              'WHERE COALESCE(dispensou,0)=0').fetchone()
                r['nps'] = {'respostas': n[0] or 0, 'facil': round(n[1] or 0, 1),
                            'gostou': round(n[2] or 0, 1), 'faz_sentido': round(n[3] or 0, 1)}
            if _tem_tabela(c, 'portal_duvida'):
                r['duvidas_30d'] = um('SELECT COUNT(*) FROM portal_duvida WHERE criado_em>=?', (t30,))
            return r
        saida['portal'] = _bloco('portal', portal, avisos)

        def producao():
            r = {}
            if _tem_tabela(c, 'producao_fila'):
                r['producao'] = pares("SELECT COALESCE(status,'(sem)'), COUNT(*) FROM producao_fila GROUP BY status")
            if _tem_tabela(c, 'revisao_fila'):
                r['revisao'] = pares("SELECT COALESCE(status,'(sem)'), COUNT(*) FROM revisao_fila GROUP BY status")
            if _tem_tabela(c, 'pecas_agente'):
                r['pecas_30d'] = um('SELECT COUNT(*) FROM pecas_agente WHERE criado_em>=?', (_time.time() - 30 * 86400,))
            return r or None
        saida['producao'] = _bloco('producao', producao, avisos)
    finally:
        c.close()
    return saida


_central_cache: dict = {}


@router.get('/central')
def central(user: Usuario = Depends(usuario_atual)):
    """Numeros agregados da Central de Comando. Cache de 60 s (a tela atualiza sozinha).

    def (nao async): roda em thread, nao trava o worker unico."""
    agora = _time.time()
    if _central_cache.get('t', 0) > agora - 60:
        return _central_cache['d']
    d = _montar_central()
    _central_cache.update(t=agora, d=d)
    return d
