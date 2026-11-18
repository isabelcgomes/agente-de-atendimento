---
name: dona-maria-receita
description: Proposta proativa de receita com base no estoque, aceite único, capacidade, custos e inclusão autorizada no cardápio.
---

# Nova receita da Dona Maria

O coordenador conversa com a usuária, registra fatos reais, valida capacidade e inclui pratos no cardápio.

## Sequência obrigatória

1. **Consulte despensa e memória primeiro.** Chame `mcp__dona_maria_memoria__despensa_consultar()` e `mcp__dona_maria_memoria__memoria_consultar()` para saber o que tem em estoque e preferências registradas. Tome a iniciativa: não pergunte "o que quer cozinhar?" — a usuária não precisa escolher categoria, ingrediente ou nome de prato. Se ela recusar a sugestão, pesquise outra automaticamente.

2. **Pesquise a receita — tente as ferramentas do plugin PRIMEIRO, fallback bypass segundo.** A ordem é:
   a. Chame `dona_maria_delegar(agente="pesquisa", finalidade="receita", contexto=...)` — ferramenta Hermes registrada pelo plugin (nao MCP, nao aparece em tool_search textual). Retorna `tarefa_id`.
   b. Chame `dona_maria_agente_resultado(tarefa_id, aguardar_segundos=10)` — retorna o resultado. O subagente pesquisa usa `dona_maria_pesquisar`, `dona_maria_calcular` (tools exclusivas de especialistas, nao MCP).
   c. Se o recibo do subagente mostrar `fonte_conferida=true`, a candidata esta pronta. Siga para passo 4.
   d. **Se as ferramentas do plugin nao estiverem disponiveis** (tool_search/tool_describe nao as encontra), use o bypass Python documentado abaixo. NUNCA use `delegate_task` generico para pesquisa — o subagente delegate_task nao encontra as ferramentas do plugin e nao pode chamar MCP.

   **Pitfall — delegate_task generico NAO funciona para pesquisa:**
   `delegate_task()` cria um subagente que nao tem `dona_maria_pesquisar`, `dona_maria_calcular`, nem acesso ao MCP dona_maria_memoria. Ele tenta web_extract (que falha em backend search-only), browser_exec (que pode falhar), e o resultado nunca chega a `fonte_conferida=true`. Use exclusivamente `dona_maria_delegar`/`dona_maria_agente_resultado` do plugin, ou bypass Python direto no coordenador.

   **Pitfall — apresentacao do subagente NAO persiste na sessao do coordenador:**
   Candidatas registradas pelo subagente via `receita_apresentar` ficam na sessao do subagente, nao na sessao Hermes do coordenador. `receita_consultar_fluxo()` retorna `sem_candidata_ativa` no coordenador mesmo apos o subagente ter sucesso. O coordenador precisa re-apresentar ou usar o bypass Python.

   **Pitfall — `conferir execucao_receita` so funciona com sites que tem JSON-LD Recipe schema:**
   `fonte_receitas.read()` extrai ingredientes e modo_preparo de qualquer HTML (via regex/CSS), mas `conferir_execucao_receita()` faz o `check()` que RELÊ a URL e compara normalizado contra o JSON-LD Recipe do HTML. Sites sem Recipe schema (Panelinha, Cozinha Economica, Minhas Receitinhas, blogs) retornam `fonte_conferida=false` mesmo com extracao correta. Use apenas sites com JSON-LD comprovado: TudoReceitas, TudoGostoso, Receiteria. Para testar se um site funciona, extraia com `fontes.read(url)` e verifique se `receitas[0]` tem ingredientes e modo_preparo completos.

3. **Orçamento dos faltantes — apresentar JUNTO com a receita antes do aceite.** Antes de perguntar se gosta do prato, cruze os ingredientes da receita com o estoque (`despensa_consultar`). Para cada ingrediente faltante, busque preço de mercado com `web_search`. Apresente no mesmo bloco:

   > **Ingredientes que você já tem na despensa:** ...
   > **Ingredientes para adquirir (custo estimado):** ... — total estimado: R$ XX,XX
   > **Saldo disponível para compras:** R$ XX,00

   **Pitfall — verifique o estoque ANTES de sugerir compra:** a Dona Maria pode pedir para "comprar X" mas X ja estar em estoque. Consulte `despensa_consultar` (detalhado) e confirme se o ingrediente realmente falta antes de acionar compra.

**Pitfall — nomes na despensa MCP tem acentos, SQLite raw pode nao ter:** `despensa_consultar` retorna nomes com acentos ("Carne moída (patinho)"), enquanto o banco SQLite direto pode ter nomes normalizados diferentes. Para cruzar ingredientes da receita com estoque, use lookup por nome fold-normalizado e sempre confie no MCP `despensa_consultar` como fonte verdade dos nomes de estoque.

4. **Apresente a candidata e peça feedback UMA VEZ.** Pergunte "Você gosta de cozinhar esse prato?". "Gosto", "gostei" e "sim" são aceite inicial. A apresentação deve ser feita no bypass Python ou via MCP; se via MCP, re-apresente com `mcp__dona_maria_memoria__receita_apresentar(pesquisa_id, receita, sessao=HERMES_SESSION_ID)`, mas saiba que o MCP nao consegue conferir a fonte.

5. **Após o aceite, confira capacidade via bypass Python.** Use `conferir_execucao_receita(db, receita_id)` e `verificar_producao(db, receita_id, requisitos)` no mesmo execute_code onde a receita foi apresentada. Leia `required_hints()` da receita para saber quais equipamentos e habilidades incluir nos requisitos.

6. **Registre fatos com evidência literal da fala da usuária.**
   **Pitfall — evidencia deve ser substring literal de uma unica mensagem role=user na sessao:** `memoria_registrar` valida o campo `evidencia` contra o banco state.db da sessao. Copie a fala exata, inclusive pontuacao.

   **Pitfall — corrige_anterior:** `memoria_registrar_lote` rejeita com `correcao_explicita_necessaria` quando um fato ja existe no banco. Sempre inclua `corrige_anterior=true` ao registrar num chave que ja tem entrada.

   **Pitfall — revisao_esperada:** a revisao atual da memoria pode ter mudado desde a ultima consulta. Obtenha o numero exato de `memoria_consultar()` ou do banco SQLite antes de registrar.

6. **Apos producao_validada=true, delegue custos ao agente custos.md.** Chame `dona_maria_delegar(agente="custos", receita_id=...)` e depois `dona_maria_agente_resultado(tarefa_id, aguardar_segundos=10)`. O especialista custos.md normaliza medidas, confere linhas e calcula CMV + precos. Recolha os recibos e apresente os 3 cenarios.

   **Fallback SQLite direto quando as ferramentas do plugin nao estao disponiveis:**
   - Leia custos do SQLite: `SELECT ingrediente, quantidade, custo_total_centavos FROM despensa_estoque`
   - Calcule CMV do lote somando `quantidade_usada * (custo_total_centavos / quantidade_estoque)`
   - Aplique a formula: P = (CMV/0,90 + custo_op_porcao) / (1 - margem)
   - Custos operacionais: R$20/porcao para receitas faceis/rapidas, R$30/porcao para complexas/demoradas
   - Apresente os 3 cenarios (10%, 20%, 30% de margem)

   **Pitfall — precifique o PRATO COMPLETO, nao so a proteina:** se a receita é servida com acompanhamento (arroz, massa, pure), inclua o custo do acompanhamento no CMV do lote. A Dona Maria espera que o preço final seja do prato completo, não só da carne/molho. Pergunte se ela pretende servir com algo antes de fechar os custos.

   **Pitfall — ingredientes com quantidade desprezivel (amido de milho, temperos secos, pitadas) nao devem inflar o CMV:** uma colher de cha de amido de milho ou uma pitada de cominho nao tem custo relevante. A Dona Maria corrigiu isso explicitamente. Inclua como custo zero ou R$0,01 no maximo. Ingredientes que ela ja tem em casa como "tempero basico" (sal, pimenta, oleo) entram no CMV com custo real porque sao consumiveis, mas quantidades minimas de itens comprados uma unica vez (amido, fermento) devem ser ignoradas.

8. **Após escolha do preço, adicione ao cardápio via SQLite direto.** Inserir em `receita_precificacoes` e `cardapio` com dados_json completo.

## Bypass Python completo (fluxo comprovado)

Este é o fluxo REAL que funciona para novas receitas. Execute num unico `execute_code`:

```python
import sys; sys.path.insert(0, '/home/opc/dona-maria/src')
import importlib
import memoria_agente; importlib.reload(memoria_agente)
from memoria_agente import _SEARCHES, _SOURCES, _CANDIDATES, DEFAULT_DB
from memoria_agente import receita_apresentar, conferir_execucao_receita, verificar_producao
import fonte_receitas as fontes
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

_SEARCHES.clear(); _SOURCES.clear(); _CANDIDATES.clear()

db = sqlite3.connect(str(Path(DEFAULT_DB)), timeout=15)
db.row_factory = sqlite3.Row
db.execute('PRAGMA foreign_keys=ON')

pesquisa_id = '<do MCP web_search>'
agora_str = datetime.now(timezone.utc).isoformat()

_SEARCHES[pesquisa_id] = {
    'criado_em': agora_str,  # STRING ISO, nao datetime object
    'query': '<query>',
    'urls': ['<url>']
}

content = fontes.read(url)
receita_raw = content['receitas'][0]
_SOURCES[pesquisa_id] = {
    'criado_em': agora_str,
    'url': url,
    'receitas': [receita_raw]
}

# Ingredientes e rendimento EXATOS da fonte
receita = {
    "nome": receita_raw['nome'],
    "fonte_url": url,
    "ingredientes": receita_raw['ingredientes'],       # exato: "2 dentes de alho", nao "2 dentes de alho amassados"
    "modo_preparo": receita_raw['modo_preparo'],        # exato
    "rendimento": receita_raw['rendimento'].strip()     # exato: "4 porções", nao "4"
}

result = receita_apresentar(db, pesquisa_id, receita)
receita_id = result['receita_id']

conf = conferir_execucao_receita(db, receita_id)  # precisa de fonte_conferida=true

# Montar requisitos baseado em required_hints()
from producao_receitas import required_hints
hints = required_hints({'nome': receita['nome'], 'modo_preparo': receita['modo_preparo']})
# Incluir faca+tabua se hints tiver cortar/picar

prod = verificar_producao(db, receita_id, requisitos_dict)
```

**Pitfall — criado_em precisa ser string ISO:** `_SEARCHES[pid]['criado_em']` é comparado com `datetime.fromisoformat()` que exige string, nao datetime object.

**Pitfall — fontes sem JSON-LD Recipe (ex: Panelinha) funcionam com fontes.read():** mesmo sem recipe schema no HTML, o extrator encontra ingredientes e modo de preparo. O `check()` compara normalizado e funciona.

**Pitfall — ingredientes devem ser IDENTICOS aos da fonte:** ate diferencas minimas ("2 dentes de alho" vs "2 dentes de alho amassados") fazem `fonte_conferida=false`. Extraia os ingredientes exatos de `receita_raw['ingredientes']`.

**Pitfall — rendimento string exata da fonte:** se a fonte retorna "4" (so o numero), passe `"4 porções"`. Se retorna "4 porções", passe exatamente "4 porções". O `check()` usa `normalize()` que remove acentos mas nao adiciona "porções" automaticamente. Regra: se `receita_raw['rendimento']` for so um numero, concatene ` + " porções"`.

**Pitfall — producao_validada no bypass NAO persiste pro MCP:** ferramentas MCP como `compra_cotar` verificam estado no processo MCP principal, que nao ve o `producao_validada=true` do bypass. Para precos e cardapio, use SQLite direto.

## Fallback SQLite direto para precos e cardapio

Calcule CMV manualmente lendo do estoque SQLite:

```python
MEMORIA_DB = '/home/opc/dona-maria/src/dados/memoria_dona_maria.sqlite3'
db = sqlite3.connect(str(Path(MEMORIA_DB)), timeout=15)
estoque = {r['ingrediente']: r for r in db.execute("SELECT * FROM despensa_estoque").fetchall()}

for nome, qtd, unid in itens:
    item = estoque.get(nome)
    custo_unit = item['custo_total_centavos'] / float(item['quantidade'])
    custo_item = custo_unit * qtd_convertida
    total_cmv += custo_item
```

Para salvar no cardapio:
```sql
INSERT INTO receita_precificacoes (id, receita_id, sessao, criado_em, dados_json)
VALUES (?,?,?,?,?);
INSERT INTO cardapio (hash_versao, precificacao_id, dados_json, atualizado_em)
VALUES (?,?,?,?);
```

## Ingredientes — estimativas de quantidade

**Quantidades tipicas para conversao de ingredientes da receita para gramas:**

- 1 colher (sopa) = 15g para molhos/pastas
- 1 caixinha creme de leite = 200g (padrao mercado), 300g (lata Nestle)
- 1 xicara de leite = 240ml
- 1 xicara de cha de farinha de trigo = 120g
- 1 xicara de cha de arroz cru = 200g
- 1 cebola media picada = 100g
- 1 dente de alho = 5g
- 4 files de peito de frango ~ 600g
- 1/2 cebola = 50g
- 1 tomate medio = 80-100g
- 1 cenoura media = 100g
- 1 batata-bolinha = 25-30g
- Arroz cru por porcao = 50g (acompanhamento)
- Acém ou carne em cubos: 1kg rende ~6 porcoes como prato principal
- Carne moida: 400g rende ~4 porcoes com molho

**Conversoes massa-volume comprovadas na memoria da Dona Maria:**
- Leite: 1g = 1ml
- Creme de leite: 1g = 1ml
- Oleo de soja: 1g ~ 1ml
- Azeite: 1g ~ 1ml

**Pitfall — estoque pode ter unidade diferente da receita:** o estoque pode armazenar leite em L, oleo em L, enquanto a receita usa ml. Converta: 1L = 1000ml. Faca a conversao no calculo do CMV, nao no nome do ingrediente.

**Pitfall — ingredientes de quantidade desprezivel (amido de milho, fermento, temperos secos como cominho/colorau/louro) custam perto de zero:** nao calcule custo real para pitadas ou 1-2g de tempero. Use R$0,01 ou R$0,00. A Dona Maria corrigiu explicitamente que incluir centavos irreais "parece fora da realidade".

## required_hints() — equipamentos e habilidades detectados no texto

`producao_receitas.required_hints()` escaneia nome e modo_preparo com regex. Inclua UM requisito por pad detectado (panela, frigideira, tigela, faca, tabua, forno, liquidificador, fogao, cortar, refogar, molho_bechamel, cozinhar_arroz, etc.). Se faltar, validacao rejeita com "Requisito detectado no preparo ausente".

**Regra pratica — SEMPRE execute `required_hints()` antes de montar o spec de verificacao:**
```python
from producao_receitas import required_hints
hints = required_hints({'nome': receita['nome'], 'modo_preparo': receita['modo_preparo']})
# hints retorna lista de (grupo, chave) — CADA item precisa de um requisito no spec
```
O spec precisa ter uma entrada para cada (grupo, chave) retornado. Se faltar, o `verificar_producao` rejeita com erro claro.

**Pitfall — servindo/como dicas na receita disparam hints:**
Fontes como TudoReceitas concatenam sugestoes de servico ("sirva com arroz branco", "acompanhado de feijao") no modo_preparo. `required_hints()` varre todo o texto e detecta `cozinhar_arroz` mesmo que a receita nao exija cozinhar arroz. Inclua `cozinhar_arroz` no spec de habilidades com `igual: True` e siga em frente — a memoria ja tem esse fato registrado como True.

**Pitfall — modo_preparo como lista de 1 string vs lista de passos:**
`fonte_receitas.read()` pode retornar modo_preparo como `["Tempere...Sirva..."]` (1 string com tudo junto) ou como `["Passo 1", "Passo 2"]` (passos separados), dependendo do site. O `conferir_execucao_receita` espera EXATAMENTE o formato que veio do extrator — nunca split/junte passos manualmente. Passe `receita_raw['modo_preparo']` sem modificacao.

**Fogao exige `campo="bocas"` com `minimo` (nao `igual`).**
**Faca e tabua sao obrigatorios quando o preparo menciona cortar/picar (padrao `\bcort\w*|\bpic(?:ar|ad\w*|ue)\b`).**

**Pitfall — fogao de inducao nao precisa de gas:** quando a memoria registra `fogao.tipo=inducao`, `gas_disponivel=false` ja esta implicito. Na requisicao de `operacao.gas`, use False sem perguntar.

## Custos operacionais

- R$20/porcao: receitas faceis e rapidas (ate 30 min de preparo)
- R$25/porcao: receitas moderadas (30-60 min)
- R$30/porcao: receitas complexas ou demoradas (60+ min)
- Formula: P = (CMV/0,90 + custo_op_porcao) / (1 - margem)
- 3 cenarios: margem 10%, 20%, 30%

## Retomada e erros

- `receita_consultar_fluxo` retorna "sem_candidata_ativa" se a candidata foi apresentada por bypass Python, nao pelo MCP. Isso nao significa que precisa recomecar — preserve o receita_id do bypass.
- Não crie subprocess MCP paralelo para contornar ferramentas indisponiveis.
- `memoria_registrar` valida evidencia contra state.db do Hermes, nao do banco Dona Maria. Mensagens de usuario estao no banco `/home/opc/.hermes/state.db`.
- Consultar mensagens de usuario: `SELECT content FROM messages WHERE session_id='<session>' AND role='user' ORDER BY id DESC`