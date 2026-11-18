---
name: dona-maria-admin
description: Administracao, reset e manutencao dos dados da Dona Maria
---

# Administracao do Sistema Sabor da Maria

Use quando o usuario pedir reset, limpeza, ou manutencao dos dados da Dona Maria
(despensa, orcamento, cardapio, equipamentos, habilidades, fatos operacionais).

## Regras gerais de evidencia

- O backend `dona_maria_memoria__memoria_registrar` e `memoria_registrar_lote`
  EXIGEM que o campo `evidencia` seja uma CITACAO LITERAL da fala do usuario
  nesta sessao. Texto inventado ou parafraseado gera erro
  `declaracao_real_necessaria`.
- O backend valida evidencia contra o historico do usuario via
  `user_evidence()` em `fluxo_receitas.py`. A comparacao normaliza ambos os
  lados com NFKD + stripping de combining marks (U+0300-U+036f) + casefold,
  entao acentos e caixa-alta/baixa sao ignorados na correspondencia. Tanto
  "orcamento" quanto "orçamento" funcionam.
- Use a mensagem mais recente do usuario como evidencia. A normalizacao cobre
  acentos e case, mas caracteres especiais (R$, cifrao) e pontuacao importam
  — a evidencia precisa ser um substring da fala original.
- Para revogar um fato existente, SEMPRE use `corrige_anterior: true`.
  Sem essa flag, o backend retorna `correcao_explicita_necessaria` e rejeita
  todo o lote.
- Para confirmar/alterar um fato existente, tambem use `corrige_anterior: true`.
  Um registro sem corrige_anterior em cima de fato confirmado gera o mesmo erro.
- Quando o lote e rejeitado, o backend retorna `indice_fato` (0-indexed) e
  `lote_revertido: true`. Nenhum fato do lote foi gravado.
- Chaves devem ser snake_case, sem acentos, ate 100 caracteres.

## Procedimento de reset para teste do zero

### 1. Cardapio (receitas)
- Consultar estado atual via `memoria_consultar()` — retorna revisao e lista de
  receitas_disponiveis.
- Registrar lote revogando cada receita com:
  - grupo: "receitas", status: "revogado", valor_json: null
  - corrige_anterior: true
  - evidencia: citacao literal do pedido do usuario

### 2. Habilidades
- Cada habilidade e um fato separado no grupo "habilidades".
- Revogar uma a uma com corrige_anterior: true.
- Chaves comuns: carbonara, corte_brunoise, massa_fresca, molho_bechamel,
  molho_sugo, ponto_da_carne, selar_carne.

### 3. Equipamentos
- Cada equipamento e um fato no grupo "equipamentos".
- Revogar com corrige_anterior: true.
- Chaves comuns: fogao, panelas, utensilios_manuais, batedeira, air_fryer,
  liquidificador, micro_ondas, panela_pressao, forno_eletrico, abridor_massas,
  fatiador_massas.

### 4. Operacao (tempo, energia, espaco, conversoes)
- Fatos no grupo "operacao".
- Chaves comuns: energia_disponivel, gas_disponivel, bancada_livre,
  espaco_geladeira_litros, tempo_por_cozinhada_minutos,
  molho_tomate_conversao, molho_tomate_conversao_massa_volume,
  leite_conversao_massa_volume.

### 5. Financeiro / Orcamento
- O backend CALCULA total_utilizado e saldo_disponivel a partir da tabela SQL
  `despensa_controle` no arquivo `/home/opc/dona-maria/src/dados/memoria_dona_maria.sqlite3`.
- Um fato registrado via `memoria_registrar_lote` com `valor_json` contendo
  `saldo_disponivel` NAO atualiza essa tabela SQL. O orcamento e calculado
  dos campos `orcamento_inicial_centavos` e `saldo_centavos`.
- Para RESETAR o orcamento: conectar no SQLite diretamente e executar
  `UPDATE despensa_controle SET saldo_centavos = 8000 WHERE id = 1`.
- Para ZERAR historico de compras: `DELETE FROM despensa_compras`,
  `DELETE FROM despensa_cotacoes`, `DELETE FROM producao_cotacoes`.
- Nao ha tool MCP para resetar orcamento ou apurar compras — o reset manual
  via SQL e a unica maneira.

### 6. Estoque fisico
- O estoque fica em /home/opc/dona-maria/src/despensa_dona_maria.xlsx.
- O backend nao tem tool para alterar estoque bulk — so compra_autorizar
  acrescenta. Para manter estoque, nao mexa.

## Corrigindo o backend (fluxo_receitas.py)

A validacao `user_evidence()` em `/home/opc/dona-maria/src/fluxo_receitas.py`
deve normalizar acentos e case. Se o backend estiver fazendo comparacao exata
(`evidence.strip() in row['content']`), corrigir para:

```python
def user_evidence(evidence, session=None):
    import re, unicodedata
    def normalize(s):
        return re.sub(r'[\u0300-\u036f]', '', unicodedata.normalize('NFKD', s)).casefold()
    if not isinstance(evidence, str) or not evidence.strip():
        return False
    e = normalize(evidence.strip())
    return any(
        row['role'] == 'user' and e in normalize(row['content'])
        for row in messages(session))
```

Apos editar `fluxo_receitas.py`, REINICIAR o servidor MCP. O Hermes gerencia
o ciclo de vida — usar `manage_connections(action='enable', connectors=[{'name': 'dona_maria_memoria', 'mcp': True}])`.
O `enable` recria o processo que carrega o modulo. Um simples pkill nao basta
porque o Hermes relanca o processo antigo da config em cache.

## Pitfalls

- Nao use evidencia inventada. O backend valida que a citacao corresponde
  a uma mensagem real do usuario nesta sessao.
- Um lote com 30+ itens funciona, mas cada fato com corrige_anterior=true
  consome uma revisao. O lote todo avanca a revisao em N saltos.
- O orcamento real esta no SQLite (`despensa_controle`), nao no fato
  registrado via memoria_registrar. O fato e apenas decorativo/documental.
- Nao tentar registrar o orcamento como valor_json simples (80) — o esperado
  e um objeto {"orcamento_inicial": 80.0, "saldo_disponivel": 80.0, ...}.
- Nao confundir: a ferramenta que REGISTRA fatos e memoria_registrar_lote.
  A que APROVA receitas e receita_aprovar. A que VERIFICA producao e
  receita_verificar_producao. Cada uma tem seu proposito.

## Referencias

- skill `dona-maria-receita`: fluxo de nova receita
- /home/opc/dona-maria/src/conversoes_medidas_dona_maria.json: tabela de conversoes
- /home/opc/dona-maria/src/despensa_dona_maria.xlsx: estoque fisico
- /home/opc/dona-maria/src/dados/memoria_dona_maria.sqlite3: banco real
  (tabela despensa_controle para orcamento, despensa_compras para historico)
- /home/opc/dona-maria/src/fluxo_receitas.py: funcao `user_evidence()` — validacao
  de evidencia contra historico da sessao. Modificar requer restart do MCP.
