# Copyright (c) 2017-2020 Renata Hodovan, Akos Kiss.
#
# Licensed under the BSD 3-Clause License
# <LICENSE.rst or https://opensource.org/licenses/BSD-3-Clause>.
# This file may not be copied, modified, or distributed except
# according to those terms.

import json
import random

from os.path import dirname, join

from grammarinator.runtime import *

from HTMLGenerator import HTMLGenerator


with open(join(dirname(__file__), 'html.json')) as f:
    tags = json.load(f)

tag_names = list(tags.keys())


class HTMLCustomGenerator(HTMLGenerator):

    attr_stack = []
    tag_stack = []
    tags = set()

    # Customize the function generated from the htmlTagName parser rule to produce valid tag names.
    def htmlTagName(self):
        current = self.create_node(UnparserRule(name='htmlTagName'))
        name = random.choice(tags[self.tag_stack[-1]]['children'] or tag_names if self.tag_stack else tag_names)
        self.tag_stack.append(name)
        current += UnlexerRule(src=name)
        self.tag_stack.append(name)
        return current

    # Customize the function generated from the htmlAttributeName parser rule to produce valid attribute names.
    def htmlAttributeName(self):
        current = self.create_node(UnparserRule(name='htmlAttributeName'))
        name = random.choice(list(tags[self.tag_stack[-1]]['attributes'].keys()) or ['""'])
        self.attr_stack.append(name)
        current += UnlexerRule(src=name)
        return current

    # Customize the function generated from the htmlAttributeValue parser rule to produce valid attribute values
    # to the current tag and attribute name.
    def htmlAttributeValue(self):
        current = self.create_node(UnparserRule(name='htmlAttributeValue'))
        current += UnlexerRule(src=random.choice(tags[self.tag_stack[-1]]['attributes'].get(self.attr_stack.pop(), ['""']) or ['""']))
        return current

    def endOfHtmlElement(self):
        self.tag_stack.pop()

    # You probably want to rewrite this with a distinct CSS fuzzer.
    def style_sheet(self):
        return UnlexerRule(src='* { background: green; }')
