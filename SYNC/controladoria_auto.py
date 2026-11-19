# -*- coding: utf-8 -*-
"""CONTROLADORIA AUTOMATICA - roda sozinha no Agendador do Windows (ou no cron da VPS).

Le as intimacoes pendentes do cache do SYNC, recalcula o motor (prazos, titularidade,
sugestao de conclusao) e decide, para cada uma:

  CONCLUIR   ciencia pura / copia da parte contraria / duplicata / ja conferida
             (as mesmas regras do autotrato.py): marca tratada no SYNC.        -> nivel >= 1
  CADASTRAR  prazo NOSSO, confianca >= limiar, vencimento calculado, ato especifico:
             abre a cadeia de tarefas e o prazo fatal na agenda. A intimacao NAO e
             marcada tratada: uma pessoa confere e conclui na tela.            -> nivel >= 2
  HUMANO     todo o resto, com o motivo.

Chaves no config/.env:
  CONTROLADORIA_AUTO_NIVEL=0        0 desligado | 1 conclui ciencia | 2 + cadastra prazos
  CONTROLADORIA_AUTO_CONFIANCA=0.8  confianca minima do motor para CADASTRAR

Uso:
  python SYNC/controladoria_auto.py               simula (nao grava nada no SYNC nem em tarefas)
  python SYNC/controladoria_auto.py --aplicar     executa o que o nivel do .env permite
  python SYNC/controladoria_auto.py --atualizar   puxa do SYNC antes (se o servidor nao estiver rodando)

Sempre grava a planilha do que fez (ou faria) em SISTEMA/backend/relatorios_controladoria/.
"""
import argparse
import csv
import json
import logging
import os
import secrets
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
BACKEND = RAIZ / 'SISTEMA' / 'backend'
sys.path.insert(0, str(BACKEND))
# este script nao emite login; so evita que o config recuse subir sem JWT_SECRET
os.environ.setdefault('JWT_SECRET', secrets.token_urlsafe(48))
sys.path.insert(0, str(Path(__file__).resolve().parent))          # controladoria_pecas
from dotenv import load_dotenv  # noqa: E402
load_dotenv(RAIZ / 'config' / '.env')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('controladoria_auto')

from app.config import settings  # noqa: E402
from app.routers import controladoria as C  # noqa: E402
from app import motor_tarefas as MT  # noqa: E402

POR = 'Controladoria automatica'
# Atos genericos demais para abrir cadeia sem uma pessoa olhar.
ATOS_SO_HUMANO = {'MANIFESTACAO GENERICA', 'PROCESSO SIGILOSO'}
PASTA_REL = BACKEND / 'relatorios_controladoria'


def _cfg(nome, padrao):
    v = getattr(settings, nome, None)
    if v in (None, ''):
        v = os.environ.get(nome, padrao)
    return v


def _garantir_tabelas(con):
    con.execute('CREATE TABLE IF NOT EXISTS sync_intim_tratamento (id INTEGER PRIMARY KEY, tratada INTEGER, '
                'por TEXT, regra TEXT, motivo TEXT, criado_em REAL)')
    con.execute('CREATE TABLE IF NOT EXISTS controladoria_auto_log (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                'intimacao_id INTEGER, processo TEXT, acao TEXT, motivo TEXT, aplicado INTEGER, '
                'detalhe TEXT, em REAL)')
    con.commit()


def decidir(con, it, m, nivel, conf_min, uf):
    """(acao, motivo, etapas). Nunca grava nada."""
    if it.get('tratada'):
        return 'IGNORAR', 'ja tratada no SYNC', []
    if not m:
        return 'HUMANO', 'motor sem resultado', []
    if m.get('sugestao_conclusao'):
        s = m['sugestao_conclusao']
        return 'CONCLUIR', '%s: %s' % (s.get('rotulo'), s.get('motivo')), []
    falta = []
    if m.get('titularidade') != 'NOSSO':
        falta.append('titularidade %s' % m.get('titularidade'))
    if (m.get('confianca') or 0) < conf_min:
        falta.append('confianca %.2f < %.2f' % (m.get('confianca') or 0, conf_min))
    if not m.get('vencimento'):
        falta.append('sem vencimento calculado')
    elif str(m['vencimento'])[:10] <= datetime.now().strftime('%Y-%m-%d'):
        # vencido ou vence hoje: abrir cadeia de 5 etapas nao adianta; e caso de alguem olhar JA
        falta.append('URGENTE: prazo vence/venceu em %s' % str(m['vencimento'])[:10])
    if m.get('trilha') not in ('PECA', 'MEIO'):
        falta.append('trilha %s' % m.get('trilha'))
    if not (m.get('cliente') or '').strip():
        falta.append('cliente nao identificado (sem pasta para a peca)')
    if m.get('ato') in ATOS_SO_HUMANO:
        falta.append('ato generico (%s)' % m.get('ato'))
    if m.get('sigiloso') or m.get('teor_fora_do_djen'):
        falta.append('sigilo ou teor fora do DJEN')
    if (m.get('conferencia') or {}).get('condicao') or (m.get('conferencia') or {}).get('vigiar_em'):
        falta.append('prazo condicionado ou da parte contraria')
    if falta:
        return 'HUMANO', '; '.join(falta), []
    ja = MT.ja_cadastrado(con, it['id'], it.get('processo'))
    if ja.get('tarefas'):
        return 'IGNORAR', 'cadeia ja cadastrada (%d tarefa(s))' % len(ja['tarefas']), []
    if C._cadeia_gemea(con, it['id'], it.get('processo'), m):
        return 'IGNORAR', 'gemea de cadeia ja aberta no processo', []
    cad = MT.cadeia_para(m, it, uf=uf)
    etapas = cad.get('etapas') or []
    if not etapas:
        return 'HUMANO', 'motor nao montou cadeia: %s' % (cad.get('motivo_sem_cadeia') or '?'), []
    return 'CADASTRAR', 'prazo nosso, confianca %.2f, %s' % (m.get('confianca'), m.get('ato')), etapas


def aplicar(con, it, m, acao, motivo, etapas, uf):
    if acao == 'CONCLUIR':
        C._sync_tratar(it['id'])
        con.execute('INSERT OR REPLACE INTO sync_intim_tratamento(id,tratada,por,regra,motivo,criado_em) '
                    'VALUES(?,?,?,?,?,?)', (int(it['id']), 1, POR, 'auto-ciencia', motivo[:300], time.time()))
        con.commit()
        C._cache_marca_tratadas([it['id']], True)
        try:
            import controladoria_pecas as CP
            card = CP.card_para_mero_expediente(it.get('processo'), it.get('data'), motivo)
            return 'tratada no SYNC' + (', card %s em MERO EXPEDIENTE' % card if card else ', sem card no Infinitum')
        except Exception as e:
            return 'tratada no SYNC (Infinitum falhou: %s)' % str(e)[:120]
    if acao == 'CADASTRAR':
        from app import agenda_google as AG
        rel = MT.cadastrar(con, it, m, etapas, POR, True, AG if AG.disponivel() else None, uf,
                           'Cadastro automatico da controladoria. Conferir e concluir a intimacao na tela.')
        txt = '%d tarefa(s)%s' % (len(rel.get('tarefas') or []),
                                  ', PF %s' % rel['pf'].get('evento_id') if rel.get('pf') else '')
        # o que e peca (trilha PECA ou peticao de meio) vai para a fila da producao automatica
        if m.get('trilha') in ('PECA', 'MEIO'):
            import controladoria_pecas as CP
            cp = CP.con()
            try:
                txt += ', peca na fila #%s' % CP.enfileirar(cp, it, m)
            finally:
                cp.close()
        return txt
    return ''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--aplicar', action='store_true', help='executa (sem isto so simula)')
    ap.add_argument('--atualizar', action='store_true', help='puxa do SYNC antes de decidir')
    ap.add_argument('--nivel', type=int, help='sobrepoe CONTROLADORIA_AUTO_NIVEL')
    a = ap.parse_args()

    nivel = a.nivel if a.nivel is not None else int(_cfg('CONTROLADORIA_AUTO_NIVEL', 0) or 0)
    conf_min = float(_cfg('CONTROLADORIA_AUTO_CONFIANCA', 0.8) or 0.8)
    aplicando = a.aplicar and nivel > 0
    log.info('nivel %s | confianca minima %.2f | %s', nivel, conf_min, 'APLICANDO' if aplicando else 'SIMULACAO')

    if a.atualizar:
        C.refresh_cache()

    con = C._cache_con()
    con.row_factory = sqlite3.Row
    _garantir_tabelas(con)
    MT.garantir(con)
    MT.garantir_historico(con)

    intims = (C.cache_get('intimacoes') or {}).get('intimacoes') or []
    if not intims:
        log.warning('cache sem intimacoes (o servidor ja puxou do SYNC? use --atualizar)')
        return
    motor = C._motor_sombra(con, intims)
    C._sugerir_conclusao(con, intims, motor)

    linhas, cont = [], {}
    for it in intims:
        m = motor.get(it.get('id')) or {}
        uf = C._uf_da_intimacao(it)
        try:
            acao, motivo, etapas = decidir(con, it, m, nivel, conf_min, uf)
        except Exception as e:
            acao, motivo, etapas = 'HUMANO', 'erro ao decidir: %s' % str(e)[:150], []
        permitido = (acao == 'CONCLUIR' and nivel >= 1) or (acao == 'CADASTRAR' and nivel >= 2)
        resultado = ''
        if aplicando and permitido:
            try:
                resultado = aplicar(con, it, m, acao, motivo, etapas, uf)
                MT.historico(con, 'intimacao', it['id'], 'auto-' + acao.lower(), resultado or motivo, POR)
            except Exception as e:
                resultado = 'FALHOU: %s' % str(getattr(e, 'detail', e))[:200]
                log.warning('%s %s: %s', acao, it.get('id'), resultado)
            con.execute('INSERT INTO controladoria_auto_log(intimacao_id,processo,acao,motivo,aplicado,detalhe,em) '
                        'VALUES(?,?,?,?,?,?,?)', (it.get('id'), it.get('processo'), acao, motivo[:500],
                                                 0 if resultado.startswith('FALHOU') else 1, resultado, time.time()))
            con.commit()
        elif acao in ('CONCLUIR', 'CADASTRAR'):
            resultado = 'faria (simulacao)' if permitido or not aplicando else 'fora do nivel %s' % nivel
        cont[acao] = cont.get(acao, 0) + 1
        linhas.append({
            'id': it.get('id'), 'processo': it.get('processo'), 'tribunal': it.get('tribunal'),
            'data': str(it.get('data') or '')[:10], 'ato': m.get('ato'), 'trilha': m.get('trilha'),
            'titularidade': m.get('titularidade'), 'confianca': m.get('confianca'),
            'vencimento': m.get('vencimento'), 'fatal_interno': m.get('prazo_fatal_interno'),
            'cliente': m.get('cliente'), 'acao': acao, 'motivo': motivo, 'resultado': resultado,
            'etapas': ' > '.join('%s (%s)' % (e.get('tipo'), e.get('para') or '') for e in etapas),
            'resumo': str(it.get('resumo') or it.get('texto') or '')[:300].replace('\n', ' '),
        })
    con.close()

    PASTA_REL.mkdir(exist_ok=True)
    for velho in PASTA_REL.glob('controladoria_*.csv'):   # guarda 7 dias
        if time.time() - velho.stat().st_mtime > 7 * 86400:
            velho.unlink(missing_ok=True)
    arq = PASTA_REL / ('controladoria_%s_%s.csv' % (datetime.now().strftime('%Y%m%d_%H%M'),
                                                    'aplicado' if aplicando else 'simulacao'))
    with open(arq, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=list(linhas[0].keys()), delimiter=';')
        w.writeheader()
        w.writerows(linhas)
    log.info('resumo: %s | planilha: %s', json.dumps(cont, ensure_ascii=False), arq)


if __name__ == '__main__':
    main()
