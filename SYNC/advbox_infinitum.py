# -*- coding: utf-8 -*-
"""Sincroniza o ADVBOX com os bancos de dados do Infinitum (Clientes, Parte adversa, Processos,
Lancamentos, Movimentacoes), respeitando a estrutura reorganizada em nov/2026.

Regras (o Infinitum foi curado a mao depois da importacao; o sync nao pode desfazer isso):
- Chave: campo "ID ADVBOX" (advbox_id). Movimentacao nao tem id no ADVBOX: a chave e
  processo + data + titulo.
- Cria o que falta. NUNCA apaga: o que sumiu do ADVBOX so e listado.
- Campos que so o ADVBOX controla (ids, datas de saida/encerramento, honorarios recebidos...)
  sao sempre atualizados. Os demais so sao PREENCHIDOS quando estao vazios.
- Nunca grava vazio por cima de valor preenchido. Titulo de cliente/processo existente nao muda.
- Processo: status/fase so mudam quando a fase do ADVBOX muda (a ultima vista fica na linha
  "Fase no Advbox:" das observacoes).
- Pessoa nova do ADVBOX com origem "PARTE CONTRARIA" vai para o banco Parte adversa.
- Tribunal/Orgao vem do SYNC (atendedireito); sem ele, o tribunal sai do numero CNJ.
- Gravacao parcial: o PUT do Infinitum mescla, entao so os campos alterados sao enviados.
- Sem --executar so simula e mostra as diferencas.

    python advbox_infinitum.py                       simula tudo
    python advbox_infinitum.py --executar            grava tudo
    python advbox_infinitum.py clientes processos    so essas entidades
    python advbox_infinitum.py movimentacoes --desde 2026-10-30
    python advbox_infinitum.py --executar --max 1    no maximo 1 escrita por entidade (teste)
"""
import datetime as dt
import json
import os
import re
import sys
from collections import Counter

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RAIZ, 'INTEGRACOES'))

import advbox_integration as AB  # noqa: E402
import infinitum_integration as INF  # noqa: E402

DIAS_MOVIMENTACOES = 15   # janela padrao das movimentacoes (diario)


def _iso(d):
    """'01/03/1960' ou '1960-03-01 10:00:00' -> '1960-03-01'."""
    s = str(d or '').strip()
    if not s:
        return None
    if '/' in s[:10]:
        try:
            return dt.datetime.strptime(s[:10], '%d/%m/%Y').date().isoformat()
        except ValueError:
            return s
    return s[:10]


def _moeda(v):
    return None if v is None else '%.2f' % float(v)


def _sim(v):
    return 'sim' if v else None


def _txt(v):
    return '' if v is None else str(v).strip()


def _vazio(v):
    return _txt(v) in ('', '[]', '{}', 'null')


def _up(s):
    return INF._norm(s).upper()


# ------------------------------------------------------------------ contexto (indices)

CTX = {
    'tipos_processo': {},   # lawsuit_id -> tipo de acao
    'clientes': {},         # advbox_id -> (record_id, titulo) no banco Clientes
    'adversas': {},         # advbox_id -> (record_id, titulo) no banco Parte adversa
    'usuarios': {},         # primeiro nome normalizado -> user_id do Infinitum
    'sync': {},             # numero CNJ -> processo do SYNC
}


def _digitos(s):
    return re.sub(r'\D', '', _txt(s))


def _indice(nome_banco):
    """advbox_id -> (record_id, titulo); tambem 'doc:<cpf>' e 'nome:<nome>' para achar mesclados."""
    banco = INF.achar_banco(nome_banco)
    out = {}
    for r in INF.listar_registros(banco['id']) if banco else []:
        alvo = (r['id'], _txt(r.get('title')))
        v = r.get('values') or {}
        for k in _ids_reg(r):
            out.setdefault(k, alvo)
        for doc in re.split(r'[,;/]', _txt(v.get('documento') or v.get('cpf_cnpj'))):
            if len(_digitos(doc)) >= 11:
                out.setdefault('doc:' + _digitos(doc), alvo)
        if _txt(v.get('nome') or r.get('title')):
            out.setdefault('nome:' + _up(v.get('nome') or r.get('title')), alvo)
    return out


def _ja_cadastrada(c):
    """Pessoa do ADVBOX sem o proprio ID no Infinitum, mas ja mesclada pela curadoria (mesmo CPF/nome)."""
    if len(_digitos(c.get('identification'))) >= 11:   # com CPF/CNPJ: so pelo documento (homonimos)
        chaves = ['doc:' + _digitos(c.get('identification'))]
    else:
        chaves = ['nome:' + _up(c.get('name'))]
    for indice in (CTX['clientes'], CTX['adversas']):
        for k in chaves:
            if k in indice:
                return indice, indice[k]
    return None, None


def carregar_indices():
    CTX['clientes'] = _indice('Clientes')
    CTX['adversas'] = _indice('Parte adversa')
    try:
        r = INF.requisitar('GET', 'organization-users', {'organization_id': INF.org_id()})
        nomes = Counter()
        pares = []
        for u in (r.get('data') if isinstance(r, dict) else r) or []:
            primeiro = _up(u.get('name') or (u.get('user') or {}).get('first_name')).split(' ')[0]
            if primeiro:
                nomes[primeiro] += 1
                pares.append((primeiro, u.get('user_id') or (u.get('user') or {}).get('id')))
        CTX['usuarios'] = {n: uid for n, uid in pares if nomes[n] == 1}   # so nome sem ambiguidade
    except Exception as e:
        print('   (usuarios do Infinitum indisponiveis: %s)' % str(e)[:80])
    CTX['sync'] = carregar_sync()


def carregar_sync():
    """Processos do SYNC (tribunal, orgao julgador, distribuicao). Sem token: vazio."""
    token = os.getenv('SYNC_API_TOKEN')
    if not token:
        return {}
    import requests
    base = (os.getenv('SYNC_API_BASE') or 'https://api.sync.atendedireito.app/v1').rstrip('/')
    out, pagina = {}, 1
    try:
        while True:
            r = requests.get(base + '/processos', headers={'Authorization': 'Bearer ' + token},
                             params={'pagina': pagina, 'itens': 200}, timeout=60)
            r.raise_for_status()
            lote = r.json().get('processos') or []
            out.update({p['numero']: p for p in lote if p.get('numero')})
            if len(lote) < 200:
                return out
            pagina += 1
    except Exception as e:
        print('   (SYNC indisponivel, tribunal so pelo numero CNJ: %s)' % str(e)[:80])
        return out


# ------------------------------------------------------------------ regras de dominio

# "Origem do lead" (select do Infinitum) a partir da origem livre do ADVBOX.
# Opcoes do campo: Site, Indicação, Redes sociais, WhatsApp, Já era cliente, Telefone, Evento, Outro.
# PARTE CONTRARIA nao e lead: fica vazio. O texto original segue em "Origem (Advbox)".
ORIGEM_LEAD = (
    ('ANUNCIO', 'Redes sociais'), ('FACEBOOK', 'Redes sociais'), ('INSTAGRAM', 'Redes sociais'),
    ('INDICA', 'Indicação'), ('PALESTRA', 'Evento'), ('SITE', 'Site'), ('GOOGLE', 'Site'),
    ('WHATS', 'WhatsApp'), ('TELEFONE', 'Telefone'), ('ESCRITORIO', 'Já era cliente'),
)
ORIGEM_MANUAL_LIVRE = ('', 'Outro')   # so estes valores podem ser trocados pelo sync


def _origem_lead(origem):
    o = _up(origem)
    if not o or 'PARTE CONTRARIA' in o:
        return None
    return next((v for k, v in ORIGEM_LEAD if k in o), 'Outro')


def _parte_contraria(c):
    return 'PARTE CONTRARIA' in _up(c.get('origin'))


# grupo do ADVBOX -> "Área do direito" (so preenche vazio; a curadoria manda)
AREA_GRUPO = (
    ('TRABALH', 'Trabalhista'), ('ALIMENTOS', 'Família e Sucessões'), ('INVENTARIO', 'Família e Sucessões'),
    ('FAMILIA', 'Família e Sucessões'), ('PREVIDENC', 'Previdenciário'), ('TRIBUT', 'Tributário'),
    ('PENAL', 'Criminal'), ('CRIMINAL', 'Criminal'), ('ADMINISTRATIVO', 'Administrativo'),
    ('EMPRESARIAL', 'Empresarial'), ('CONSUMIDOR', 'Consumidor'), ('CIVEL', 'Cível'),
)

UF_TR = ('', 'AC', 'AL', 'AP', 'AM', 'BA', 'CE', 'DF', 'ES', 'GO', 'MA', 'MT', 'MS', 'MG', 'PA', 'PB', 'PR',
         'PE', 'PI', 'RJ', 'RN', 'RS', 'RO', 'RR', 'SC', 'SE', 'SP', 'TO')


def tribunal_cnj(numero):
    """NNNNNNN-DD.AAAA.J.TR.OOOO -> 'TJSP', 'TRT2', 'TRF3', 'TST'..."""
    m = re.match(r'^\d{7}-\d{2}\.\d{4}\.(\d)\.(\d{2})\.\d{4}$', _txt(numero))
    if not m:
        return None
    j, tr = int(m.group(1)), int(m.group(2))
    uf = UF_TR[tr] if 0 < tr < len(UF_TR) else ''
    if j == 8 and uf:
        return 'TJ' + uf
    if j == 5:
        return 'TST' if tr == 0 else 'TRT%d' % tr
    if j == 4 and tr:
        return 'TRF%d' % tr
    if j == 6 and uf:
        return 'TRE-' + uf
    if j == 9 and uf:
        return 'TJM-' + uf
    return {1: 'STF', 3: 'STJ', 7: 'STM'}.get(j)


def status_fase(step, stage):
    """Etapa/fase do ADVBOX -> (Status do processo, Fase) do Infinitum."""
    e, f = _up(step), _up(stage)
    if e == 'ARQUIVAMENTO' or 'ARQUIVADO' in f or 'NAO DISTRIBU' in f:
        return 'Arquivado', 'Arquivado'
    if e == 'RECURSAL':
        return 'Em Recurso', 'Recursal'
    if 'EXECUC' in e:
        return 'Ativo', 'Execução'
    if e == 'JUDICIAL' and any(x in f for x in ('PROTOCOLAD', 'CITAC', 'CONTESTAC', 'AUDIENCIA', 'SENTENC',
                                                 'REPLICA', 'PERICIA', 'INSTRUC', 'AGUARDANDO LIDE')):
        return 'Ativo', 'Conhecimento'
    return 'Ativo', None   # pre-ajuizamento (roteiro, documentos, inicial...): fase fica vazia


RE_FASE_OBS = re.compile(r'^Fase no Advbox:\s*(.*)$', re.M)


def _vinculos(alvos):
    vistos, itens = set(), []
    for rid, titulo in alvos:
        if rid not in vistos:
            vistos.add(rid)
            itens.append({'record_id': rid, 'title': titulo})
    return json.dumps(itens, ensure_ascii=False) if itens else None


# ------------------------------------------------------------------ mapeamentos
# Cada um devolve (titulo, sempre, so_vazio): "sempre" segue o ADVBOX; "so_vazio" so preenche.

def _proc_rotulo(l):
    lid = l.get('lawsuit_id')
    return l.get('process_number') or '%s (#%s)' % (CTX['tipos_processo'].get(lid, ''), lid)


def cliente(c, atual=None):
    processos = '\n'.join(_proc_rotulo(l) for l in reversed(c.get('lawsuits') or []))
    sempre = {'advbox_id': c['id'], 'origem_advbox': c.get('origin'),
              'cadastrado_advbox_em': _iso(c.get('created_at')), 'processos_advbox': processos,
              'origem': _origem_lead(c.get('origin'))}
    so_vazio = {
        'nome': c.get('name'), 'documento': c.get('identification'), 'rg': c.get('document'),
        'celular': c.get('cellphone'), 'telefone': c.get('phone') or c.get('cellphone'), 'email': c.get('email'),
        'sexo': c.get('gender'), 'estado_civil': c.get('civil_status'), 'profissao': c.get('occupation'),
        'endereco': c.get('street'), 'cep': c.get('postalcode'), 'cidade': c.get('city'), 'uf': c.get('state'),
        'data_nascimento': _iso(c.get('birthdate')), 'ctps': c.get('number_ctps'), 'pis': c.get('number_pis'),
        'observacoes': c.get('notes'),
    }
    return c['name'], sempre, so_vazio


def adversa(c, atual=None):
    processos = '\n'.join(_proc_rotulo(l) for l in reversed(c.get('lawsuits') or []))
    sempre = {'advbox_id': c['id'], 'processos_advbox': processos}
    so_vazio = {'nome': c.get('name'), 'cpf_cnpj': c.get('identification'),
                'telefone': c.get('phone') or c.get('cellphone'), 'email': c.get('email'),
                'observacoes': c.get('notes')}
    return c['name'], sempre, so_vazio


def processo(p, atual=None):
    atual = atual or {}
    clientes = p.get('customers') or []
    nomes = ', '.join(c.get('name') or '' for c in clientes)
    numero = p.get('process_number') or p.get('protocol_number') or p.get('type') or ''
    titulo = ' - '.join(x for x in (numero, nomes) if x)
    s = CTX['sync'].get(p.get('process_number') or '') or {}
    status, fase = status_fase(p.get('step'), p.get('stage'))

    vinc_cli, vinc_adv = [], []
    for c in clientes:
        i = str(c.get('customer_id'))
        if i in CTX['clientes']:
            vinc_cli.append(CTX['clientes'][i])
        elif i in CTX['adversas']:
            vinc_adv.append(CTX['adversas'][i])
        else:   # cadastro do ADVBOX mesclado em outro registro (mesmo CPF/nome)
            indice, alvo = _ja_cadastrada({'name': c.get('name'), 'identification': c.get('identification')})
            if alvo:
                (vinc_cli if indice is CTX['clientes'] else vinc_adv).append(alvo)
    resp = _up(p.get('responsible')).split(' ')[0] if p.get('responsible') else ''
    area = next((v for k, v in AREA_GRUPO if k in _up(p.get('group'))), None)

    sempre = {
        'advbox_id': p['id'], 'numero_protocolo': p.get('protocol_number'), 'pasta_codigo_interno': p.get('folder'),
        'saida_producao': p.get('exit_production'), 'saida_execucao': p.get('exit_execution'),
        'cadastrado_advbox_em': _iso(p.get('created_at')), 'data_de_encerramento': _iso(p.get('status_closure')),
        'honorarios_recebidos': _moeda(p.get('fees_money')),
    }
    so_vazio = {
        'numero_do_processo': p.get('process_number'),
        'tribunal': s.get('tribunal') or tribunal_cnj(p.get('process_number')),
        'orgao_vara': s.get('orgao_julgador'),
        'area_do_direito': area, 'tipo_de_acao': p.get('type'),
        'advogado_responsavel': CTX['usuarios'].get(resp),
        'cliente': _vinculos(vinc_cli), 'parte_adversa': _vinculos(vinc_adv),
        'data_distribuicao_protocolo': _iso(s.get('data_distribuicao')) or _iso(p.get('process_date')),
        'honorarios': _moeda(p.get('fees_expec')),
        'status_do_processo': status, 'fase': fase,
    }

    # "Fase no Advbox:" das observacoes guarda a ultima fase vista; mudou -> status/fase seguem o ADVBOX
    obs = _txt(atual.get('observacoes'))
    stage = _txt(p.get('stage'))
    m = RE_FASE_OBS.search(obs)
    arquivado = status == 'Arquivado' or _txt(atual.get('status_do_processo')) == 'Arquivado'
    if stage and (m or not arquivado) and (not m or _up(m.group(1)) != _up(stage)):
        # sem a linha e ja arquivado: nao anota (evita poluir centenas de processos antigos)
        linha = 'Fase no Advbox: %s' % stage
        sempre['observacoes'] = RE_FASE_OBS.sub(linha, obs, count=1) if m else '\n'.join(x for x in (obs, linha) if x)
        if m:   # a fase mudou de verdade no ADVBOX (nao e so a primeira anotacao)
            sempre['status_do_processo'] = status
            if fase:
                sempre['fase'] = fase
    return titulo, sempre, so_vazio


TIPO_LANC = {'income': 'Receita', 'revenue': 'Receita', 'receita': 'Receita',
             'expense': 'Despesa', 'despesa': 'Despesa'}


def lancamento(t, atual=None):
    tipo = TIPO_LANC.get(str(t.get('entry_type') or '').lower(), t.get('entry_type'))
    return ('%s: %s' % (tipo, t.get('description') or ''))[:200], {   # a importacao cortou em 200
        'advbox_id': t['id'], 'tipo': tipo, 'descricao': t.get('description'), 'valor': _moeda(t.get('amount')),
        'vencimento': _iso(t.get('date_due')), 'pagamento': _iso(t.get('date_payment')),
        'competencia': t.get('competence'), 'categoria': t.get('category'), 'centro_custo': t.get('cost_center'),
        'banco_debito': t.get('debit_bank'), 'banco_credito': t.get('credit_bank'),
        'responsavel': t.get('responsible'), 'cliente': t.get('name'), 'cliente_cpf': t.get('identification'),
        'numero_processo': t.get('process_number'), 'advbox_processo_id': t.get('lawsuit_id'),
        'interno': _sim(t.get('is_internal')), 'recorrente': _sim(t.get('is_recurrent')),
    }, {}


def movimentacao(m, atual=None):
    return '%s %s' % (m.get('date'), m.get('title') or ''), {
        'advbox_processo_id': m.get('lawsuit_id'), 'numero_processo': m.get('process_number'),
        'cliente': m.get('customers'), 'data': _iso(m.get('date')),
        'titulo': m.get('title'), 'descricao': m.get('title'),
    }, {}


# ------------------------------------------------------------------ motor

def _comparavel(k, v):
    t = _txt(v)
    if k == 'processos_advbox':   # lista: a ordem nao importa
        return tuple(sorted(x.strip() for x in t.split('\n') if x.strip()))
    return t


def diferencas(atual, sempre, so_vazio):
    """{campo: (atual, novo)} do que deve ser gravado."""
    dif = {}
    for k, v in sempre.items():
        if _vazio(v) and not _vazio(atual.get(k)) and k != 'data_de_encerramento':
            continue   # nao apaga dado (reabertura no ADVBOX limpa o encerramento: essa pode)
        if _comparavel(k, atual.get(k)) != _comparavel(k, v):
            dif[k] = (atual.get(k), v)
    for k, v in so_vazio.items():
        if k not in dif and not _vazio(v) and _vazio(atual.get(k)):
            dif[k] = (atual.get(k), v)
    # "Origem do lead" pode ter sido escolhida a mao no Infinitum: so preenche vazio/"Outro"
    if 'origem' in dif and (_txt(atual.get('origem')) not in ORIGEM_MANUAL_LIVRE or dif['origem'][1] is None):
        del dif['origem']
    return dif


def atualizar_registro(registro, dif, campos, titulo=None):
    """PUT parcial (o Infinitum mescla): so os campos alterados, pelo slug e pelo id do campo."""
    vals = {}
    for slug, (_, v) in dif.items():
        v = '' if v is None else str(v)
        vals[slug] = v
        if slug in campos:
            vals[campos[slug]] = v
    corpo = {'values': vals}
    if titulo is not None:
        corpo['title'] = titulo
    return INF.requisitar('PUT', 'database-records/%s' % registro['id'], json=corpo)


def sincronizar(nome_banco, itens, mapear, chave_registro, chave_item, executar, maximo,
                atualizar=True, atualizar_titulo=False, ids_advbox=None):
    """ids_advbox: todas as chaves do ADVBOX (para listar o que sumiu quando `itens` e so uma parte)."""
    banco = INF.achar_banco(nome_banco)
    if not banco:
        print('!! banco "%s" nao existe no Infinitum' % nome_banco)
        return
    campos = {c['field_id']: c['id'] for c in INF.listar_campos_banco(banco['id'])}
    existentes = {}   # chave -> lista (o ADVBOX tem andamentos identicos repetidos de verdade)
    registros = INF.listar_registros(banco['id'])
    chaves_de = {}    # record id -> chaves (registro mesclado tem varios IDs ADVBOX)
    for r in registros:
        ks = chave_registro(r)
        ks = [ks] if isinstance(ks, str) else (ks or [])
        chaves_de[r['id']] = ks
        for k in ks:
            existentes.setdefault(k, []).append(r)
    total_inf = sum(1 for r in registros if chaves_de[r['id']])
    usados, tocados = {}, set()
    criar, mudar, chaves = [], [], set()
    for it in itens:
        k = chave_item(it)
        chaves.add(k)
        lista = existentes.get(k) or []
        i = usados.get(k, 0)
        reg = lista[i] if i < len(lista) else None
        if reg is not None:
            usados[k] = i + 1
        titulo, sempre, so_vazio = mapear(it, (reg or {}).get('values'))
        if reg is None:
            if sempre is None:   # o mapeamento pediu para nao criar (pessoa ja mesclada)
                continue
            valores = {k2: v for k2, v in {**so_vazio, **sempre}.items() if k2 in campos and not _vazio(v)}
            criar.append((titulo, valores))
        elif atualizar and reg['id'] not in tocados:   # mesclado: so o primeiro item do ADVBOX atualiza
            tocados.add(reg['id'])
            if len(chaves_de[reg['id']]) > 1:
                sempre = {k2: v for k2, v in sempre.items() if k2 != 'advbox_id'}   # preserva "id1, id2"
            dif = diferencas(reg.get('values') or {}, sempre, so_vazio)
            dif = {k2: v for k2, v in dif.items() if k2 in campos}
            novo_titulo = titulo if atualizar_titulo and _txt(reg.get('title')) != _txt(titulo) else None
            if dif or novo_titulo:
                mudar.append((reg, novo_titulo, dif))
    print('\n== %s: ADVBOX %d | Infinitum %d | criar %d | atualizar %d'
          % (nome_banco, len(itens), total_inf, len(criar), len(mudar)))
    if atualizar:   # so nas entidades com ID ADVBOX; andamentos sao uma janela de dias
        todas = ids_advbox if ids_advbox is not None else chaves
        sumiram = [r for r in registros if chaves_de[r['id']] and not any(k in todas for k in chaves_de[r['id']])]
        if sumiram:
            print('   ! %d no Infinitum que nao existem mais no ADVBOX (nao apagados):' % len(sumiram))
            for r in sumiram[:10]:
                print('     - %s (ID ADVBOX %s)' % (_txt(r.get('title'))[:80], ', '.join(chaves_de[r['id']])))
    for titulo, _ in criar[:15]:
        print('   + %s' % titulo[:110])
    cont = Counter(k for *_, dif in mudar for k in dif)
    cont.update('titulo' for _, t, _ in mudar if t)
    print('   campos que mudam: %s' % dict(cont))
    for reg, t, dif in mudar[:8]:
        print('   ~ %s: %s' % (_txt(reg.get('title'))[:60],
                                '; '.join('%s %r->%r' % (k, str(a)[:40] if a else a, str(b)[:40])
                                          for k, (a, b) in list(dif.items())[:4]) or 'titulo -> %r' % t))
    if not executar:
        return
    feitos = erros = 0
    for titulo, valores in criar[:maximo] if maximo else criar:
        try:
            INF.criar_registro(banco['id'], titulo, valores)
            feitos += 1
        except Exception as e:
            erros += 1
            print('   ERRO criar %s: %s' % (titulo[:60], str(e)[:150]))
    for reg, t, dif in mudar[:maximo] if maximo else mudar:
        try:
            atualizar_registro(reg, dif, campos, t)
            feitos += 1
        except Exception as e:
            erros += 1
            print('   ERRO atualizar %s: %s' % (_txt(reg.get('title'))[:60], str(e)[:150]))
    print('   gravados %d, erros %d' % (feitos, erros))


def _ids_reg(r):
    """'17905118, 17961830' -> ['17905118', '17961830'] (registros mesclados pela curadoria)."""
    return [x for x in re.split(r'[\s,;/]+', _txt((r.get('values') or {}).get('advbox_id'))) if x]


def _id_reg(r):
    return _ids_reg(r) or None


def _chave_mov(lid, data, titulo):
    return '%s|%s|%s' % (_txt(lid), _txt(data)[:10], ' '.join(_txt(titulo).split()))


def movimentacoes_advbox(desde, processos=None):
    """Andamentos >= desde de todos os processos (uma chamada por processo)."""
    out = []
    processos = processos or AB.listar_processos()
    for i, p in enumerate(processos, 1):
        try:
            r = AB.requisitar('GET', 'movements/%s' % p['id'])
        except Exception as e:
            print('   processo %s: %s' % (p['id'], str(e)[:80]))
            continue
        for m in (r.get('data') or []) if isinstance(r, dict) else []:
            if _iso(m.get('date')) and _iso(m.get('date')) >= desde:
                out.append(m)
        if i % 100 == 0:
            print('   ... %d/%d processos lidos, %d andamentos' % (i, len(processos), len(out)))
    return out


def sincronizar_pessoas(executar, maximo):
    """Clientes do ADVBOX -> banco Clientes ou Parte adversa (onde a curadoria colocou)."""
    pessoas = AB.listar_clientes()
    todos = {str(c['id']) for c in pessoas}
    cli, adv = CTX['clientes'], CTX['adversas']
    para_cli = [c for c in pessoas if str(c['id']) in cli or (str(c['id']) not in adv and not _parte_contraria(c))]
    para_adv = [c for c in pessoas if str(c['id']) not in cli and (str(c['id']) in adv or _parte_contraria(c))]
    mescladas = [c for c in pessoas if str(c['id']) not in cli and str(c['id']) not in adv and _ja_cadastrada(c)[1]]
    if mescladas:
        print('\n   %d pessoas do ADVBOX ja estao no Infinitum por CPF/nome (mescladas; nao recria):' % len(mescladas))
        for c in mescladas[:10]:
            print('     = %s (ID ADVBOX %s) -> %s' % (c.get('name'), c['id'], _ja_cadastrada(c)[1][1]))
    ids_mescladas = {c['id'] for c in mescladas}

    def so_novas(mapear):
        def f(c, atual=None):
            if atual is None and c['id'] in ids_mescladas:
                return c.get('name'), None, None
            return mapear(c, atual)
        return f
    sincronizar('Clientes', para_cli, so_novas(cliente), _id_reg, lambda c: str(c['id']), executar, maximo,
                ids_advbox=todos)
    sincronizar('Parte adversa', para_adv, so_novas(adversa), _id_reg, lambda c: str(c['id']), executar, maximo,
                ids_advbox=todos)


def main():
    INF._carregar_env()
    args = sys.argv[1:]
    executar = '--executar' in args
    maximo = int(args[args.index('--max') + 1]) if '--max' in args else None
    desde = args[args.index('--desde') + 1] if '--desde' in args else \
        (dt.date.today() - dt.timedelta(days=DIAS_MOVIMENTACOES)).isoformat()
    todas = ['clientes', 'processos', 'lancamentos', 'movimentacoes']
    quais = [a for a in args if a in todas] or todas
    print('%s | %s' % ('GRAVANDO' if executar else 'SIMULACAO', ', '.join(quais)))
    processos = AB.listar_processos() if {'clientes', 'processos', 'movimentacoes'} & set(quais) else []
    CTX['tipos_processo'].update({p['id']: p.get('type') or '' for p in processos})
    if {'clientes', 'processos'} & set(quais):
        carregar_indices()

    if 'clientes' in quais:
        sincronizar_pessoas(executar, maximo)
        if executar and 'processos' in quais:   # pessoas novas precisam entrar nos vinculos
            CTX['clientes'], CTX['adversas'] = _indice('Clientes'), _indice('Parte adversa')
    if 'processos' in quais:
        sincronizar('Processos', processos, processo, _id_reg, lambda p: str(p['id']), executar, maximo)
    if 'lancamentos' in quais:
        sincronizar('Lancamentos', AB.listar_lancamentos(), lancamento, _id_reg, lambda t: str(t['id']),
                    executar, maximo, atualizar_titulo=True)
    if 'movimentacoes' in quais:
        print('\nlendo andamentos do ADVBOX desde %s (uma consulta por processo)...' % desde)
        movs = movimentacoes_advbox(desde, processos)
        sincronizar('Movimentacoes', movs, movimentacao,
                    lambda r: _chave_mov((r.get('values') or {}).get('advbox_processo_id'),
                                         (r.get('values') or {}).get('data'), (r.get('values') or {}).get('titulo')),
                    lambda m: _chave_mov(m.get('lawsuit_id'), m.get('date'), m.get('title')),
                    executar, maximo, atualizar=False)


if __name__ == '__main__':
    main()
