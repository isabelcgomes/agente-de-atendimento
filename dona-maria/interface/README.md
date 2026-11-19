# Interface do Hermes — Sabor da Maria

Aplicação web local em português, com quatro telas: chat com o Hermes instalado, troca da planilha da despensa, cardápio aprovado e orçamento com cotações e compras autorizadas.

## Iniciar

Na raiz do repositório:

```bash
python3 -B dona-maria/interface/server.py
```

Abra **http://localhost:8080**. Use `--port 8081` para escolher outra porta. A interface requer Python 3.9 ou superior; o chat requer o comando `hermes` instalado, credenciais válidas e o MCP do projeto configurado. O frontend não precisa de Node ou de compilação. Fontes externas são opcionais, com alternativas locais.

Se o servidor estiver em uma VM, crie um túnel no seu computador:

```bash
ssh -L 8080:127.0.0.1:8080 usuario@servidor
```

Acesse `http://localhost:8080` no seu computador. O serviço escuta apenas em `127.0.0.1`; execute o túnel com o usuário e endereço da sua VM.

## Integração

O servidor consulta `dona-maria/src/dados/memoria_dona_maria.sqlite3` sem escrever na base operacional. O orçamento e o estoque vêm do módulo existente `despensa_compras`; o cardápio vem dos registros aprovados, inclusive formatos históricos. A tela de compras apresenta cotações pendentes ainda válidas, separadas como alternativas, e as últimas 20 compras autorizadas. Uma lista vazia indica ausência de cotações válidas, não ausência de possíveis faltantes.

O chat executa `hermes chat -q ... --format stream-json`, com diretório de trabalho em `dona-maria/src` e `HERMES_HOME` em `.hermes` na raiz deste repositório. O identificador de sessão retornado pelo Hermes é salvo; mensagens seguintes usam `--resume`. O backend apresenta texto e atividade das ferramentas, sem simular respostas. O histórico local persiste em SQLite e pode ser recuperado ao recarregar a página. “Nova conversa” cria outra sessão e mantém os dados da cozinha. Reiniciar o servidor durante uma resposta interrompe o acompanhamento daquela execução; confira o histórico e os dados antes de repetir uma operação.

A configuração MCP, o link do plugin e as referências do ambiente de dependências em `.hermes/installs/.../facts.json` foram alinhados para esta cópia do projeto. Ao mover o projeto, ajuste esses caminhos absolutos. As telas atualizam a cada 12 segundos e ao concluir uma resposta; o botão no cabeçalho permite consulta imediata.

## Trocar a despensa

Envie um `.xlsx` de até 8 MB, no formato da planilha original:

- Primeira aba: ingrediente, quantidade em estoque, unidade.
- Segunda aba: ingrediente, quantidade comprada, unidade, valor pago em reais.
- Primeira linha de cada aba: cabeçalho. Use valores nas células de dados, sem fórmulas.

A importação valida duplicatas, preços, quantidades e conversões conhecidas. Ingredientes novos podem usar g, kg, L ou ml; embalagens precisam de uma conversão cadastrada. Estoque zero é permitido. Os custos usam as quantidades efetivamente em estoque, proporcionais ao valor de compra.

A confirmação na interface substitui todo o estoque, preserva o saldo e o histórico de compras, invalida cotações pendentes e incrementa a revisão para impedir a aprovação de precificações antigas. Os preços já aprovados continuam visíveis com aviso para revisão pelo Hermes. O upload não autoriza compras e não recalcula pratos automaticamente.

Antes da troca, são salvas cópias da base SQLite e da planilha anterior em `dona-maria/src/dados/interface/backups/`. Histórico do chat e registros das importações ficam em `dona-maria/src/dados/interface/chat.sqlite3`. Esses arquivos estão no `.gitignore`. O chat e a importação são serializados para evitar a troca da despensa durante uma resposta. O tempo máximo de uma execução do chat é de dez minutos.

## Verificar

```bash
python3 -B -m unittest discover -s dona-maria/interface/tests -v
```

Os testes usam cópias temporárias da base e da planilha e verificam preservação de saldo e histórico, invalidação de cotações, rejeição de arquivos inválidos, bloqueio durante o chat e consultas sem escrita na base operacional.
