"""Leitura de páginas públicas, independente do backend de busca DDGS."""
import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = []
        self.structured = []
        self.script = None

    def handle_starttag(self, tag, attrs):
        if tag in ['script', 'style', 'noscript']:
            self.hidden.append(tag)
            if tag == 'script' and dict(attrs).get('type') == 'application/ld+json':
                self.script = []

    def handle_endtag(self, tag):
        if self.hidden and tag == self.hidden[-1]:
            self.hidden.pop()
            if tag == 'script' and self.script is not None:
                try:
                    self.structured.append(json.loads(''.join(self.script)))
                except ValueError:
                    pass
                self.script = None
        if not self.hidden:
            self.parts.append('\n')

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        elif not self.hidden:
            self.parts.append(data)


def recipe_objects(value):
    if isinstance(value, dict):
        kind = value.get('@type', [])
        if kind == 'Recipe' or isinstance(kind, list) and 'Recipe' in kind:
            yield value
        for child in value.values():
            yield from recipe_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from recipe_objects(child)


def clean(text):
    page = Page()
    page.feed(str(text))
    return re.sub(r'\s+', ' ', ''.join(page.parts)).strip()


def steps(value):
    if isinstance(value, str):
        return [clean(value)] if clean(value) else []
    if isinstance(value, list):
        return [step for item in value for step in steps(item)]
    if isinstance(value, dict):
        return steps(value.get('itemListElement') or value.get('text', ''))
    return []


def parse(html):
    page = Page()
    page.feed(html)
    recipes = []
    for obj in recipe_objects(page.structured):
        ingredients = obj.get('recipeIngredient')
        preparation = steps(obj.get('recipeInstructions'))
        if not isinstance(ingredients, list) or not ingredients or not preparation:
            continue
        if not all(isinstance(x, str) and clean(x) for x in ingredients):
            continue
        yield_value = obj.get('recipeYield', 'não informado')
        recipes.append({'nome': clean(obj.get('name', 'Receita')),
                        'ingredientes': [clean(x) for x in ingredients],
                        'modo_preparo': preparation,
                        'rendimento': clean(', '.join(map(str, yield_value)) if isinstance(yield_value, list) else yield_value)})
    return {'receitas': recipes, 'texto': ''.join(page.parts)[:20000]}


def read(url):
    repo = str(Path.home() / '.hermes' / 'hermes-agent')
    if repo not in sys.path:
        sys.path.insert(0, repo)
    # O cliente oficial valida DNS no momento da conexão, inclusive redirecionamentos.
    from tools.url_safety import create_ssrf_safe_client
    deadline = time.monotonic() + 30
    with create_ssrf_safe_client(timeout=10, follow_redirects=True, max_redirects=4, trust_env=False) as client:
        with client.stream('GET', url) as response:
            response.raise_for_status()
            if 'html' not in response.headers.get('content-type', '').lower():
                raise ValueError('A fonte não retornou HTML')
            chunks = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 2_000_000 or time.monotonic() > deadline:
                    raise ValueError('Página excedeu o limite de tamanho ou tempo')
                chunks.append(chunk)
            return parse(b''.join(chunks).decode(response.encoding or 'utf-8', errors='replace'))


def normalize(text):
    return re.sub(r'\s+', ' ', str(text)).strip().casefold()


def check(recipe, content):
    """Exige listas completas da mesma receita, evitando omissões de etapas/requisitos."""
    if isinstance(content, dict):
        for original in content.get('receitas', []):
            if all([normalize(x) for x in recipe[field]] == [normalize(x) for x in original[field]]
                   for field in ['ingredientes', 'modo_preparo']) and normalize(recipe['rendimento']) == normalize(original['rendimento']):
                return True, []
    return False, ['Ingredientes e preparo devem corresponder às listas completas de uma receita estruturada da página.']
