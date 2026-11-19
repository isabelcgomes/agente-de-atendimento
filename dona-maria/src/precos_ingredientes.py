"""Preços anunciados de produtos, com embalagem, moeda e fonte identificadas."""
import re
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import despensa_compras as pantry
from fonte_receitas import Page

RESEARCH_VERSION = 2


def product_matches(ingredient, title):
    words = re.findall(r'[a-z0-9]+', pantry.key(ingredient))
    words = [w for w in words if w not in {'de','da','do','em','e','cru','crua','moida','moido'}]
    title = pantry.key(title).replace('-', ' ')
    return bool(words) and all(re.search(r'\b'+re.escape(w[:-1] if w.endswith('s') and len(w)>3 else w)+r's?\b', title) for w in words)


def package(title):
    if re.search(r'\d\s*[xX]\s*\d', title):
        return None
    matches = {(m[1].replace(',', '.'), m[2].lower()) for m in
               re.finditer(r'\b(\d+(?:[.,]\d+)?)\s*(kg|g|ml|l)\b', title, re.I)}
    if not matches and re.search(r'\bkg\b', title, re.I):
        return '1', 'kg', 'por_peso'
    if len(matches) != 1:
        return None
    quantity, unit = matches.pop()
    return quantity, 'L' if unit == 'l' else unit, 'embalagem'


def offer(ingredient, title, value, currency, url, origin, priced_unit=None):
    if str(currency).upper() != 'BRL' or not product_matches(ingredient, title):
        return None
    content = priced_unit or package(title)
    if not content:
        return None
    try:
        price = Decimal(str(value))
        if not price.is_finite() or price <= 0 or price != price.quantize(Decimal('.01')):
            return None
    except InvalidOperation:
        return None
    return {'ingrediente':ingredient, 'produto':title, 'quantidade':content[0], 'unidade':content[1],
            'tipo_venda':content[2],
            'preco_embalagem_BRL':format(price, '.2f'), 'fonte':url, 'origem_evidencia':origin,
            'frete_BRL':None}


def structured_offers(html, ingredient, url):
    page = Page(); page.feed(html)
    def products(node):
        if isinstance(node, list):
            for item in node:
                yield from products(item)
        elif isinstance(node, dict):
            if 'Product' in ([node.get('@type')] if isinstance(node.get('@type'), str) else (node.get('@type') or [])):
                yield node
            for item in node.values():
                yield from products(item)
    result = []
    for product in products(page.structured):
        offers = product.get('offers', [])
        if isinstance(offers, dict):
            offers = [offers]
        for row in offers:
            if not isinstance(row, dict) or str(row.get('availability', '')).endswith(('OutOfStock','Discontinued')):
                continue
            value = offer(ingredient, product.get('name', ''), row.get('price'), row.get('priceCurrency'), url, 'pagina_produto')
            if value:
                result.append(value)
    return result


def snippet_offer(row, ingredient):
    text = str(row.get('title', ''))+' '+str(row.get('description', ''))
    def amount(text):
        text = re.sub(r'\s+', '', text)
        if ',' in text:
            return text.replace('.', '').replace(',', '.')
        return text
    matches = list(re.finditer(r'R\$\s*(\d+(?:\.\d{3})*,\s*\d{2}|\d+\.\s*\d{2}|\d+)\b(?![.,]\s*\d)', text))
    prices = {amount(m[1]) for m in matches}
    units = set()
    for match in matches:
        unit = re.match(r'^\s*(?:/|por\s+)\s*(?:(\d+)\s*)?(kg|g|ml|l)\b', text[match.end():], re.I)
        if unit:
            units.add((amount(match[1]), unit[1] or '1', 'L' if unit[2].lower() == 'l' else unit[2].lower()))
    priced_unit = None
    title_package = package(str(row.get('title', '')))
    if len(units) == 1:
        value, quantity, unit = units.pop()
        package_prices = prices-{value}
        if title_package and title_package[2] == 'embalagem' and len(package_prices) == 1:
            prices = package_prices
            priced_unit = title_package
        else:
            prices = {value}
            priced_unit = (quantity, unit, 'por_peso')
    elif title_package:
        priced_unit = title_package
    if len(prices) != 1:
        return None
    value = offer(ingredient, text, prices.pop(), 'BRL', row.get('url', ''), 'preco_anunciado_na_busca', priced_unit)
    if value:
        match = re.search(r'R\$\s*\d', text)
        value['produto'] = str(row.get('title', ''))[:300]
        value['trecho_preco'] = text[max(0, match.start()-100):match.end()+200]
    return value


def with_coverage(value, missing):
    result = dict(value)
    if not missing:
        result['quantidade_necessaria_pendente'] = True
        return result
    size, unit, _ = pantry.normalize(value['ingrediente'], value['quantidade'], value['unidade'])
    if unit != missing['unidade']:
        result['quantidade_necessaria_pendente'] = True
        return result
    if value.get('tipo_venda') == 'por_peso':
        fraction = pantry.number(missing['faltante'])/size
        result.update(quantidade_compra=str(fraction), unidade_compra=value['unidade'],
                      custo_itens_BRL=format((Decimal(value['preco_embalagem_BRL'])*fraction).quantize(Decimal('.01')), '.2f'),
                      quantidade_necessaria_pendente=False)
        return result
    count = int((pantry.number(missing['faltante'])/size).to_integral_value(rounding=ROUND_CEILING))
    result.update(embalagens=count, custo_itens_BRL=format(Decimal(value['preco_embalagem_BRL'])*count, '.2f'),
                  quantidade_necessaria_pendente=False)
    return result
