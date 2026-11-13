---
name: timbrado
description: Coloca qualquer texto de peça no papel timbrado da Nogueira & Tovani (Times New Roman 12, recuo de 7 cm, títulos em negrito e sublinhado, fecho com data do protocolo eletrônico e assinatura dos dois sócios).
---

# timbrado

Use SEMPRE no último passo de qualquer peça. Nunca recrie o cabeçalho: o modelo oficial é
`SISTEMA/backend/app/juridico/modelos/TIMBRADO.docx` (logo no cabeçalho, endereço no rodapé, A4).
Esse arquivo fica fora do git; a cópia-mestra está na skill `timbrado-nogueira-tovani`
(`assets/timbrado.docx`).

Como usar:
```
python .claude/skills/timbrado/montar_peca.py --texto PECA.txt --saida "Nome do Cliente - aaaa mm dd - Nome da Peça.docx"
```
O texto vai com um parágrafo por linha e sem markdown. O script reconhece sozinho:
- primeira linha de endereçamento (EXCELENTÍSSIMO / AO JUÍZO / EGRÉGIO TRIBUNAL…): negrito, sem
  recuo, seguida de 9 linhas em branco (espaço do despacho);
- "Processo nº …" ou "Para distribuição": negrito e sublinhado;
- RECORRENTE/RECORRIDA/ORIGEM/EGRÉGIA TURMA: negrito, sem recuo;
- nome da ação, PRELIMINAR e MÉRITO (RECLAMAÇÃO TRABALHISTA, RÉPLICA, RECURSO ORDINÁRIO…):
  centralizados, negrito e sublinhado;
- demais títulos (linha toda em caixa alta, "DA COMPETÊNCIA"): negrito e sublinhado, sem recuo;
- linha que começa com `>`: jurisprudência, recuo esquerdo de 3 cm, fonte 10, entrelinha simples;
- primeiro parágrafo de texto (a abertura): os nomes das partes em CAIXA ALTA saem em negrito;
- linha que começa em minúscula logo depois do nome da ação ("pelo rito ordinário, em face de…"):
  sem recuo;
- o restante: justificado, recuo de 7 cm na primeira linha, uma linha em branco entre parágrafos;
- "Termos em que, pede deferimento." e tudo o que vier depois é trocado pelo fecho padrão:
  "Termos em que, / pede deferimento.", "São Paulo, data do protocolo eletrônico." e o bloco de
  THIAGO TOVANI (OAB/SP 299.424) e WAGNER NOGUEIRA DA SILVA (OAB/SP 258.002) lado a lado.
