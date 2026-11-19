"""
Integracao com a Meta Marketing API (Facebook/Instagram Ads).

Token: System User permanente (nao expira). Guardado em config/.env como META_ADS_TOKEN.
App: Escritório Modelo Analytics. Conta padrao: META_AD_ACCOUNT no config/.env
(hoje Ca 01 Civil, act_1352494608793230). Sem ela, faltando() avisa; nao ha conta fixa.

Funcoes principais:
  get_token()                                   -> str | None
  listar_contas()                               -> [ {name, id, ...} ]
  listar_campanhas(account_id, ativas=False)    -> [ {name, status, objective, ...} ]
  insights(obj_id, level, date_preset, ...)     -> [ rows ]
  conversas(actions)                            -> int   (conversas WhatsApp)
  custo_por(cost_per_action_type, tipo)         -> float
  achar_campanha(account_id, nome_parcial)      -> dict | None
  resumo_campanha(camp_id, date_preset)         -> dict  (gasto, conversas, cpconv, ctr...)

Endpoints (Graph API v21):
  GET /me/adaccounts
  GET /{account_id}/campaigns
  GET /{object_id}/insights
"""
import os
import logging
import requests

log = logging.getLogger('integracoes.meta_ads')

BASE = 'https://graph.facebook.com/v21.0'


ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'config', '.env')


def _carregar_env():
    """Le o config/.env para o ambiente, sem sobrescrever o que ja estiver la.

    O modulo e importado antes de quem chama carregar o .env (campanhas.py so
    carrega no _prep), entao le o arquivo aqui mesmo. Sem depender do
    python-dotenv: quando ele faltava, o import seguia sem token e sem conta,
    e o modulo caia calado numa conta antiga.
    """
    if not os.path.exists(ENV_PATH):
        log.error('config/.env nao encontrado em %s', ENV_PATH)
        return
    for linha in open(ENV_PATH, encoding='utf-8-sig', errors='ignore'):
        linha = linha.strip()
        if '=' in linha and not linha.startswith('#'):
            k, v = linha.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _conta_do_env():
    """Conta de anuncios do escritorio: META_AD_ACCOUNT (ou META_ADS_ACCOUNT) no config/.env.

    Sem conta configurada devolve '' e faltando() avisa. Nunca cai numa conta
    fixa: pausar/ativar/orcamento mexeriam na conta errada.
    """
    _carregar_env()
    acc = (os.getenv('META_AD_ACCOUNT') or os.getenv('META_ADS_ACCOUNT') or '').strip()
    if not acc:
        log.error('META_AD_ACCOUNT ausente no config/.env')
        return ''
    return acc if acc.startswith('act_') else 'act_' + acc


CONTA_PADRAO = _conta_do_env()                 # Ca 01 Civil (act_1352494608793230) via .env
APP_ID = '1746103106581807'                     # Escritório Modelo Analytics

# acao que representa "lead" real numa campanha de WhatsApp
CONV = 'onsite_conversion.messaging_conversation_started_7d'
# conversa que avancou: a pessoa mandou 3 mensagens ou mais (nao so a automatica).
# E o sinal de qualidade: conversa barata que morre na 1a mensagem nao vira caso.
CONV_AVANCOU = 'onsite_conversion.messaging_user_depth_3_message_send'


def get_token():
    """Retorna o System User token do .env. None se ausente."""
    tok = os.getenv('META_ADS_TOKEN')
    if not tok:
        log.warning('META_ADS_TOKEN ausente no config/.env')
    return tok


def faltando():
    """O que falta no config/.env para falar com a Meta; None se esta tudo certo."""
    if not os.getenv('META_ADS_TOKEN'):
        return 'META_ADS_TOKEN ausente no config/.env'
    if not CONTA_PADRAO:
        return 'META_AD_ACCOUNT ausente no config/.env'
    return None


def _get(path, params=None):
    """GET na Graph API com token. Retorna dict (json) ou {} em erro."""
    tok = get_token()
    if not tok:
        return {}
    if not path or path.startswith('/'):
        log.error('GET sem conta de anuncio (%r): META_AD_ACCOUNT ausente?', path)
        return {}
    params = dict(params or {})
    params['access_token'] = tok
    try:
        r = requests.get(f'{BASE}/{path}', params=params, timeout=60)
    except Exception as e:
        log.warning(f'GET {path}: {e}')
        return {}
    if r.status_code != 200:
        log.warning(f'GET {path} {r.status_code}: {r.text[:200]}')
        return {}
    return r.json() or {}


def _post(obj_id, data):
    """POST (alteracao) na Graph API. Retorna (ok, resp_json)."""
    tok = get_token()
    if not tok:
        return False, {}
    payload = dict(data or {})
    payload['access_token'] = tok
    try:
        r = requests.post(f'{BASE}/{obj_id}', data=payload, timeout=60)
    except Exception as e:
        log.warning(f'POST {obj_id}: {e}')
        return False, {}
    ok = r.status_code in (200, 201)
    if not ok:
        log.warning(f'POST {obj_id} {r.status_code}: {r.text[:200]}')
    return ok, (r.json() or {})


# ============================================================
# LEITURA
# ============================================================

def listar_contas():
    """Todas as contas de anuncio acessiveis."""
    d = _get('me/adaccounts', {
        'fields': 'name,account_id,currency,account_status,amount_spent',
        'limit': 100,
    })
    return d.get('data', [])


def listar_campanhas(account_id=CONTA_PADRAO, ativas=False):
    """Campanhas de uma conta. ativas=True filtra so as ACTIVE."""
    d = _get(f'{account_id}/campaigns', {
        'fields': 'name,status,effective_status,objective,daily_budget,lifetime_budget',
        'limit': 200,
    })
    camps = d.get('data', [])
    if ativas:
        camps = [c for c in camps if c.get('effective_status') == 'ACTIVE']
    return camps


def insights(obj_id, level='campaign', date_preset='last_7d',
             time_increment=None, fields=None):
    """
    Insights de campanha/conjunto/anuncio.
    level: 'campaign' | 'adset' | 'ad'
    date_preset: 'today','yesterday','last_7d','last_14d','last_30d','maximum'...
    time_increment: 1 para serie diaria
    """
    if fields is None:
        fields = ('spend,impressions,reach,clicks,ctr,cpc,frequency,'
                  'actions,cost_per_action_type')
    params = {'level': level, 'fields': fields, 'date_preset': date_preset, 'limit': 500}
    if time_increment:
        params['time_increment'] = time_increment
    return _get(f'{obj_id}/insights', params).get('data', [])


def conversas(actions, tipo=CONV):
    """Extrai o numero de conversas WhatsApp de uma lista de actions."""
    for a in (actions or []):
        if a.get('action_type') == tipo:
            return int(float(a['value']))
    return 0


def avancadas(actions):
    """Conversas em que a pessoa mandou 3+ mensagens (ver CONV_AVANCOU)."""
    return conversas(actions, CONV_AVANCOU)


def custo_por(cost_per_action_type, tipo=CONV):
    """Custo por acao (ex: por conversa)."""
    for a in (cost_per_action_type or []):
        if a.get('action_type') == tipo:
            return round(float(a['value']), 2)
    return 0.0


def achar_campanha(account_id=CONTA_PADRAO, nome_parcial=''):
    """Acha a 1a campanha cujo nome contem nome_parcial (case-insensitive)."""
    for c in listar_campanhas(account_id):
        if nome_parcial.lower() in (c.get('name', '').lower()):
            return c
    return None


def resumo_campanha(camp_id, date_preset='last_7d'):
    """Resumo pronto de uma campanha no periodo."""
    rows = insights(camp_id, level='campaign', date_preset=date_preset)
    if not rows:
        return {}
    r = rows[0]
    gasto = round(float(r.get('spend', 0)), 2)
    av = avancadas(r.get('actions'))
    return {
        'gasto': gasto,
        'conversas': conversas(r.get('actions')),
        'custo_por_conversa': custo_por(r.get('cost_per_action_type')),
        'avancadas': av,
        'custo_por_avancada': round(gasto / av, 2) if av else None,
        'impressoes': int(r.get('impressions', 0)),
        'alcance': int(r.get('reach', 0)),
        'cliques': int(r.get('clicks', 0)),
        'ctr': round(float(r.get('ctr', 0)), 2),
        'cpc': round(float(r.get('cpc', 0)), 2),
        'periodo': f"{r.get('date_start')} a {r.get('date_stop')}",
    }


# ============================================================
# ALTERACAO (usar com cautela - muda a conta de verdade)
# ============================================================

def pausar(obj_id):
    """Pausa uma campanha/conjunto/anuncio."""
    return _post(obj_id, {'status': 'PAUSED'})[0]


def ativar(obj_id):
    """Ativa uma campanha/conjunto/anuncio."""
    return _post(obj_id, {'status': 'ACTIVE'})[0]


def definir_orcamento_diario(adset_id, reais):
    """Define orcamento diario de um conjunto (em reais)."""
    return _post(adset_id, {'daily_budget': int(reais * 100)})[0]


if __name__ == '__main__':
    # smoke test rapido (o .env ja foi carregado no import)
    print('Configuracao:', faltando() or 'ok', '| conta:', CONTA_PADRAO or '(nenhuma)')
    for c in listar_contas():
        print(' Conta:', c.get('name'), c.get('id'))
