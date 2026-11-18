# Conversoes Massa-Volume Autorizadas

## Molho de tomate
- 1g = 1ml (usuario autorizou)
- Se receita pedir ml/L e estoque estiver em g, usar a unidade que o backend espera (L).
- Registrar o fato na memoria com grupo=operacao e chave=molho_tomate_conversao.

## Leite integral
- 1g = 1ml (usuario autorizou)
- Mesmo tratamento: passar em L quando backend esperar.
- Registrar na memoria com chave=leite_conversao_massa_volume.

## Conversoes culinarias padrao (usar em despensa_verificar_receita)

Estas conversoes funcionam porque o `despensa_verificar_receita` aceita gramas
diretamente — passe `quantidade` em g e `unidade` = "g" para evitar
`conversao_indefinida` do `medidas_consultar`:

| Medida | Gramas |
|---|---|
| 1 colher (sopa) | 15g |
| 1 colher (cha) | 5g |
| 1 xicara (cha) | 240ml (240g para liquidos) |
| 1 cebola media | 100g |
| 1 dente de alho | 5g |
| 1 lata creme de leite | 300g |
| 1 pitada | ~1g |

Condimentos (mostarda, ketchup, extrato de tomate) nao tem conversao no banco
de medidas. Use 15g/colher sopa e passe direto em g.

## Regra geral
- Nao usar medidas_consultar para essas conversoes — o banco de medidas do
  projeto nao tem fator e o erro trava o fluxo.
- Registrar na memoria antes para rastreabilidade, mas o backend
  despensa_verificar_receita pode ainda rejeitar. Nesse caso adaptar a
  receita (ex: usar tomate fresco no lugar de molho de tomate).
