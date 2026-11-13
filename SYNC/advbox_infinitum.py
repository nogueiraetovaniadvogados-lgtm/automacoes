# -*- coding: utf-8 -*-
"""Sincroniza o ADVBOX com os bancos de dados do Infinitum (Clientes, Processos, Lancamentos,
Movimentacoes), no mesmo formato da importacao de 01/11/2026.

- Chave: campo "ID ADVBOX" (advbox_id). Movimentacao nao tem id no ADVBOX: a chave e
  processo + data + titulo.
- Cria o que falta e atualiza o que mudou. NUNCA apaga: o que sumiu do ADVBOX so e listado.
- Sem --executar so simula e mostra as diferencas.

    python advbox_infinitum.py                       simula tudo
    python advbox_infinitum.py --executar            grava tudo
    python advbox_infinitum.py clientes lancamentos  so essas entidades
    python advbox_infinitum.py movimentacoes --desde 2026-10-30
    python advbox_infinitum.py --executar --max 1    no maximo 1 escrita por entidade (teste)
"""
import datetime as dt
import os
import sys

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


# ------------------------------------------------------------------ mapeamentos

def cliente(c):
    processos = '\n'.join(l.get('process_number') or l.get('protocol_number') or '#%s' % l.get('lawsuit_id')
                          for l in c.get('lawsuits') or [])
    return c['name'], {
        'advbox_id': c['id'], 'nome': c.get('name'), 'documento': c.get('identification'),
        'rg': c.get('document'), 'celular': c.get('cellphone'),
        'telefone': c.get('phone') or c.get('cellphone'), 'email': c.get('email'),
        'sexo': c.get('gender'), 'estado_civil': c.get('civil_status'), 'profissao': c.get('occupation'),
        'endereco': c.get('street'), 'cep': c.get('postalcode'), 'cidade': c.get('city'), 'uf': c.get('state'),
        'data_nascimento': _iso(c.get('birthdate')), 'ctps': c.get('number_ctps'), 'pis': c.get('number_pis'),
        'observacoes': c.get('notes'), 'origem_advbox': c.get('origin'),
        'cadastrado_advbox_em': _iso(c.get('created_at')), 'processos_advbox': processos,
    }


def processo(p):
    clientes = p.get('customers') or []
    nomes = ', '.join(c.get('name') or '' for c in clientes)
    numero = p.get('process_number') or p.get('protocol_number')
    titulo = ' - '.join(x for x in (numero or '%s (#%s)' % (p.get('type') or '', p['id']), nomes) if x)
    return titulo, {
        'advbox_id': p['id'], 'numero_processo': p.get('process_number'), 'cliente': nomes,
        'cliente_cpf': next((c.get('identification') for c in clientes if c.get('identification')), None),
        'tipo_acao': p.get('type'), 'grupo': p.get('group'), 'responsavel': p.get('responsible'),
        'etapa': p.get('step'), 'fase': p.get('stage'), 'cadastrado_advbox_em': _iso(p.get('created_at')),
        'encerramento': p.get('status_closure'), 'observacoes': p.get('notes'),
        'saida_producao': p.get('exit_production'), 'saida_execucao': p.get('exit_execution'),
        'honorarios_previstos': _moeda(p.get('fees_expec')), 'honorarios_recebidos': _moeda(p.get('fees_money')),
        'contingencia': p.get('contingency'), 'data_processo': _iso(p.get('process_date')),
        'pasta': p.get('folder'), 'numero_protocolo': p.get('protocol_number'),
    }


TIPO_LANC = {'income': 'Receita', 'revenue': 'Receita', 'receita': 'Receita',
             'expense': 'Despesa', 'despesa': 'Despesa'}


def lancamento(t):
    tipo = TIPO_LANC.get(str(t.get('entry_type') or '').lower(), t.get('entry_type'))
    return '%s: %s' % (tipo, t.get('description') or ''), {
        'advbox_id': t['id'], 'tipo': tipo, 'descricao': t.get('description'), 'valor': _moeda(t.get('amount')),
        'vencimento': _iso(t.get('date_due')), 'pagamento': _iso(t.get('date_payment')),
        'competencia': t.get('competence'), 'categoria': t.get('category'), 'centro_custo': t.get('cost_center'),
        'banco_debito': t.get('debit_bank'), 'banco_credito': t.get('credit_bank'),
        'responsavel': t.get('responsible'), 'cliente': t.get('name'), 'cliente_cpf': t.get('identification'),
        'numero_processo': t.get('process_number'), 'advbox_processo_id': t.get('lawsuit_id'),
        'interno': _sim(t.get('is_internal')), 'recorrente': _sim(t.get('is_recurrent')),
    }


def movimentacao(m):
    return '%s %s' % (m.get('date'), m.get('title') or ''), {
        'advbox_processo_id': m.get('lawsuit_id'), 'numero_processo': m.get('process_number'),
        'cliente': m.get('customers'), 'data': _iso(m.get('date')),
        'titulo': m.get('title'), 'descricao': m.get('title'),
    }


# ------------------------------------------------------------------ motor

def _txt(v):
    return '' if v is None else str(v).strip()


def diferencas(registro, valores):
    atual = registro.get('values') or {}
    return {k: (atual.get(k), v) for k, v in valores.items() if _txt(atual.get(k)) != _txt(v)}


def atualizar_registro(registro, titulo, valores, campos):
    vals = dict(registro.get('values') or {})
    for slug, v in valores.items():
        v = '' if v is None else str(v)
        vals[slug] = v
        if slug in campos:
            vals[campos[slug]] = v
    return INF.requisitar('PUT', 'database-records/%s' % registro['id'], json={'title': titulo, 'values': vals})


def sincronizar(nome_banco, itens, mapear, chave_registro, chave_item, executar, maximo, atualizar=True):
    banco = INF.achar_banco(nome_banco)
    if not banco:
        print('!! banco "%s" nao existe no Infinitum' % nome_banco)
        return
    campos = {c['field_id']: c['id'] for c in INF.listar_campos_banco(banco['id'])}
    existentes = {}
    for r in INF.listar_registros(banco['id']):
        k = chave_registro(r)
        if k:
            existentes.setdefault(k, r)
    criar, mudar, chaves = [], [], set()
    for it in itens:
        k = chave_item(it)
        chaves.add(k)
        titulo, valores = mapear(it)
        reg = existentes.get(k)
        if reg is None:
            criar.append((titulo, valores))
        elif atualizar:
            dif = diferencas(reg, valores)
            if dif or _txt(reg.get('title')) != _txt(titulo):
                mudar.append((reg, titulo, valores, dif))
    print('\n== %s: ADVBOX %d | Infinitum %d | criar %d | atualizar %d'
          % (nome_banco, len(itens), len(existentes), len(criar), len(mudar)))
    for titulo, _ in criar[:15]:
        print('   + %s' % titulo[:110])
    for reg, titulo, _, dif in mudar[:15]:
        print('   ~ %s: %s' % (titulo[:60], '; '.join('%s %r->%r' % (k, a, b) for k, (a, b) in list(dif.items())[:4])))
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
    for reg, titulo, valores, _ in mudar[:maximo] if maximo else mudar:
        try:
            atualizar_registro(reg, titulo, valores, campos)
            feitos += 1
        except Exception as e:
            erros += 1
            print('   ERRO atualizar %s: %s' % (titulo[:60], str(e)[:150]))
    print('   gravados %d, erros %d' % (feitos, erros))


def _id_reg(r):
    return _txt((r.get('values') or {}).get('advbox_id')) or None


def _chave_mov(lid, data, titulo):
    return '%s|%s|%s' % (_txt(lid), _txt(data)[:10], ' '.join(_txt(titulo).split()))


def movimentacoes_advbox(desde):
    """Andamentos >= desde de todos os processos (uma chamada por processo)."""
    out = []
    processos = AB.listar_processos()
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

    if 'clientes' in quais:
        sincronizar('Clientes', AB.listar_clientes(), cliente, _id_reg, lambda c: str(c['id']), executar, maximo)
    if 'processos' in quais:
        sincronizar('Processos', AB.listar_processos(), processo, _id_reg, lambda p: str(p['id']), executar, maximo)
    if 'lancamentos' in quais:
        sincronizar('Lancamentos', AB.listar_lancamentos(), lancamento, _id_reg, lambda t: str(t['id']),
                    executar, maximo)
    if 'movimentacoes' in quais:
        print('\nlendo andamentos do ADVBOX desde %s (uma consulta por processo)...' % desde)
        movs = movimentacoes_advbox(desde)
        sincronizar('Movimentacoes', movs, movimentacao,
                    lambda r: _chave_mov((r.get('values') or {}).get('advbox_processo_id'),
                                         (r.get('values') or {}).get('data'), (r.get('values') or {}).get('titulo')),
                    lambda m: _chave_mov(m.get('lawsuit_id'), m.get('date'), m.get('title')),
                    executar, maximo, atualizar=False)


if __name__ == '__main__':
    main()
