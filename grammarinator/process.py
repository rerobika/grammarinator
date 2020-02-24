# Copyright (c) 2017-2020 Renata Hodovan, Akos Kiss.
#
# Licensed under the BSD 3-Clause License
# <LICENSE.rst or https://opensource.org/licenses/BSD-3-Clause>.
# This file may not be copied, modified, or distributed except
# according to those terms.

import re

from argparse import ArgumentParser
from collections import defaultdict
from contextlib import contextmanager
from math import inf
from os import getcwd, makedirs
from os.path import dirname, exists, join
from pkgutil import get_data
from shutil import rmtree

import autopep8

from antlr4 import CommonTokenStream, FileStream, ParserRuleContext

from .cli import add_antlr_argument, add_disable_cleanup_argument, add_log_level_argument, add_version_argument, process_antlr_argument, process_log_level_argument
from .parser_builder import build_grammars
from .pkgdata import __version__, default_antlr_path
from .runtime import UnlexerRule, UnparserRule


class Node(object):

    def __init__(self, id):
        self.id = id
        self.out_neighbours = []


class RuleNode(Node):
    pass


class AlternationNode(Node):
    pass


class AlternativeNode(Node):
    pass


class QuantifierNode(Node):
    pass


class GrammarGraph(object):

    def __init__(self):
        self.vertices = dict()

    def add_node(self, node):
        self.vertices[node.id] = node

    def add_edge(self, frm, to):
        assert frm in self.vertices, '{frm} not in vertices.'.format(frm=frm)
        assert to in self.vertices, '{to} not in vertices.'.format(to=to)
        self.vertices[frm].out_neighbours.append(self.vertices[to])

    def calc_min_depths(self):
        min_depths = defaultdict(lambda: inf)
        changed = True

        while changed:
            changed = False
            for ident in self.vertices:
                selector = min if isinstance(self.vertices[ident], AlternationNode) else max
                min_depth = selector([min_depths[node.id] + int(isinstance(self.vertices[node.id], RuleNode))
                                      for node in self.vertices[ident].out_neighbours if not isinstance(node, QuantifierNode)], default=0)

                if min_depth < min_depths[ident]:
                    min_depths[ident] = min_depth
                    changed = True

        # Lift the minimal depths of the alternatives to the alternations, where the decision will happen.
        for ident in min_depths:
            if isinstance(self.vertices[ident], AlternationNode):
                assert all(min_depths[node.id] < inf for node in self.vertices[ident].out_neighbours), '{ident} has an alternative that isn\'t reachable.'.format(ident=ident)
                min_depths[ident] = [min_depths[node.id] for node in self.vertices[ident].out_neighbours]

        # Remove the lifted Alternatives and check for infinite derivations.
        for ident in list(min_depths.keys()):
            if isinstance(self.vertices[ident], AlternativeNode):
                del min_depths[ident]
            else:
                assert min_depths[ident] != inf, 'Rule with infinite derivation: %s' % ident

        return min_depths


class FuzzerGenerator(object):

    def __init__(self, antlr_parser_cls, actions):
        self.antlr_parser_cls = antlr_parser_cls
        self.actions = actions

        self.indent_level = 0
        self.charset_idx = 0
        self.code_id = 0

        self.graph = GrammarGraph()

        self.current_start_range = None
        self.token_start_ranges = dict()

        self.generator_header = ''
        self.generator_body = ''
        self.generator_name = None
        self.options = dict()
        self.code_chunks = dict()
        self.labeled_alts = []

    @contextmanager
    def indent(self):
        self.indent_level += 4
        yield
        self.indent_level -= 4

    def line(self, src=None):
        return (' ' * self.indent_level) + src + '\n' if src else '\n'

    def new_code_id(self, code_type):
        code_name = '{type}_{idx}'.format(type=code_type, idx=self.code_id)
        self.code_id += 1
        return code_name

    def new_charset_name(self):
        charset_name = 'charset_{idx}'.format(idx=self.charset_idx)
        self.charset_idx += 1
        return charset_name

    def generate_prefixes(self):
        header_prefix = self.line('# Generated by Grammarinator {version}\n'.format(version=__version__))
        header_prefix += self.line('from itertools import chain')
        header_prefix += self.line('from math import inf')
        header_prefix += self.line('from grammarinator.runtime import *\n')

        superclass = self.options.get('superClass', 'Generator')
        if superclass != 'Generator':
            header_prefix += self.line('if __name__ is not None and \'.\' in __name__:')
            with self.indent():
                header_prefix += self.line('from .{superclass} import {superclass}'.format(superclass=superclass))
            header_prefix += self.line('else:')
            with self.indent():
                header_prefix += self.line('from {superclass} import {superclass}\n'.format(superclass=superclass))

        body_prefix = self.line('class {generator_name}({superclass}):\n'.format(generator_name=self.generator_name,
                                                                                 superclass=superclass))
        with self.indent():
            body_prefix += self.line('def __init__(self, **kwargs):')

            with self.indent():
                body_prefix += self.line('super().__init__(**kwargs)')
                if self.options.get('dot'):
                    body_prefix += self.line('self.any_char = self.{dot}'.format(dot=self.options['dot']))
                body_prefix += self.line()

        with self.indent():
            body_prefix += self.line('def EOF(self, *args, **kwargs):')
            with self.indent():
                body_prefix += self.line('pass\n')

        return header_prefix, body_prefix

    def find_conditions(self, node):
        if not self.actions:
            return '1'

        if isinstance(node, str):
            return node

        action_block = getattr(node, 'actionBlock', None)
        if action_block:
            if action_block() and action_block().ACTION_CONTENT() and node.QUESTION():
                return ''.join([str(child) for child in action_block().ACTION_CONTENT()])
            return '1'

        element = getattr(node, 'element', None) or getattr(node, 'lexerElement', None)
        if element:
            if not element():
                return '1'
            return self.find_conditions(element(0))

        child_ref = getattr(node, 'alternative', None) or getattr(node, 'lexerElements', None)

        # An alternative can be explicitly empty, in this case it won't have any of the attributes above.
        if not child_ref:
            return '1'

        return self.find_conditions(child_ref())

    @staticmethod
    def character_range_interval(node):
        start = str(node.characterRange().STRING_LITERAL(0))[1:-1]
        end = str(node.characterRange().STRING_LITERAL(1))[1:-1]

        return (int(start.replace('\\u', '0x'), 16) if '\\u' in start else ord(start),
                int(end.replace('\\u', '0x'), 16) if '\\u' in end else ord(end) + 1)

    @staticmethod
    def lexer_charset_interval(src):
        elements = re.split(r'(\w-\w)', src)
        ranges = []
        for element in elements:
            if not element:
                continue

            # Convert character sequences like \n, \t, etc. into a single character.
            element = bytes(element, 'utf-8').decode('unicode_escape')
            if len(element) > 1:
                if element[1] == '-' and len(element) == 3:
                    ranges.append((ord(element[0]), ord(element[2]) + 1))
                else:
                    for char in element:
                        ranges.append((ord(char), ord(char) + 1))
            elif len(element) == 1:
                ranges.append((ord(element), ord(element) + 1))
        return ranges

    def chars_from_set(self, node):
        if node.characterRange():
            return [self.character_range_interval(node)]

        if node.LEXER_CHAR_SET():
            return self.lexer_charset_interval(str(node.LEXER_CHAR_SET())[1:-1])

        if node.STRING_LITERAL():
            assert len(str(node.STRING_LITERAL())) > 2, 'Negated string literal must not be empty.'
            first_char = ord(str(node.STRING_LITERAL())[1])
            return [(first_char, first_char + 1)]

        if node.TOKEN_REF():
            src = str(node.TOKEN_REF())
            assert src in self.token_start_ranges, '{src} not in token_start_ranges.'.format(src=src)
            return self.token_start_ranges[src]

        return []

    def generate(self, lexer_root, parser_root):
        for root in [lexer_root, parser_root]:
            if root:
                self.generate_grammar(root)

        self.code_chunks.update(self.graph.calc_min_depths())
        header_prefix, body_prefix = self.generate_prefixes()

        return (header_prefix + self.generator_header + '\n\n' + body_prefix + self.generator_body).format_map(self.code_chunks)

    def generate_grammar(self, node):
        assert isinstance(node, self.antlr_parser_cls.GrammarSpecContext)

        if node.prequelConstruct():
            for prequelConstruct in node.prequelConstruct():
                if prequelConstruct.optionsSpec():
                    for option in prequelConstruct.optionsSpec().option():
                        ident = option.identifier()
                        ident = str(ident.RULE_REF() or ident.TOKEN_REF())
                        self.options[ident] = option.optionValue().getText()

        if not self.generator_name:
            self.generator_name = re.sub(r'^(.+?)(Lexer|Parser)?$', r'\1Generator', str(node.identifier().TOKEN_REF() or node.identifier().RULE_REF()))

        if node.prequelConstruct():
            for prequelConstruct in node.prequelConstruct():
                if prequelConstruct.tokensSpec():
                    id_list = prequelConstruct.tokensSpec().idList()
                    if id_list:
                        for identifier in id_list.identifier():
                            assert identifier.TOKEN_REF() is not None, 'Token names must start with uppercase letter.'
                            rule_name = str(identifier.TOKEN_REF())
                            self.graph.add_node(RuleNode(id=rule_name))

                            with self.indent():
                                self.generator_body += self.line('def {rule_name}(self):'.format(rule_name=rule_name))
                                with self.indent():
                                    self.generator_body += self.line('return self.create_node(UnlexerRule(name=\'{rule_name}\'))\n'.format(rule_name=rule_name))

            for prequelConstruct in node.prequelConstruct():
                if prequelConstruct.action() and self.actions:
                    action = prequelConstruct.action()

                    action_ident = action.identifier()
                    action_type = str(action_ident.RULE_REF() or action_ident.TOKEN_REF())
                    raw_action_src = ''.join([str(child) for child in action.actionBlock().ACTION_CONTENT()])

                    if action_type == 'header':
                        action_src = raw_action_src
                    else:
                        with self.indent():
                            action_src = ''.join([self.line(line) for line in raw_action_src.splitlines()])

                    code_id = self.new_code_id('action')
                    self.code_chunks[code_id] = action_src
                    code_pattern = '{{{code_id}}}'.format(code_id=code_id)
                    # We simply append both member and header code chunks to the generated source.
                    # It's the user's responsibility to define them in order.
                    # Both 'member' and 'members' keywords are accepted.
                    if action_type.startswith('member'):
                        self.generator_body += code_pattern
                    elif action_type == 'header':
                        self.generator_header += code_pattern

        rules = node.rules().ruleSpec()
        generator_rules = []
        self.graph.add_node(RuleNode(id='EOF'))
        for rule in rules:
            if rule.parserRuleSpec():
                self.graph.add_node(RuleNode(id=str(rule.parserRuleSpec().RULE_REF())))
                generator_rules.append(rule.parserRuleSpec())
            elif rule.lexerRuleSpec():
                self.graph.add_node(RuleNode(id=str(rule.lexerRuleSpec().TOKEN_REF())))
                generator_rules.append(rule.lexerRuleSpec())
            else:
                assert False, 'Should not get here.'

        for mode_spec in node.modeSpec():
            for lexer_rule in mode_spec.lexerRuleSpec():
                self.graph.add_node(RuleNode(id=str(lexer_rule.TOKEN_REF())))
                generator_rules.append(lexer_rule)

        with self.indent():
            for rule in generator_rules:
                self.generator_body += self.generate_single(rule, None)

        if node.grammarType().PARSER() or not (node.grammarType().LEXER() or node.grammarType().PARSER()):
            with self.indent():
                self.generator_body += self.line('default_rule = {name}\n'.format(name=generator_rules[0].RULE_REF()))

    def generate_single(self, node, parent_id):
        if isinstance(node, (self.antlr_parser_cls.ParserRuleSpecContext, self.antlr_parser_cls.LexerRuleSpecContext)):
            parser_rule = isinstance(node, self.antlr_parser_cls.ParserRuleSpecContext)
            node_type = UnparserRule if parser_rule else UnlexerRule
            rule_name = str(node.RULE_REF() if parser_rule else node.TOKEN_REF())

            # Mark that the next lexerAtom has to be saved as start range.
            if not parser_rule:
                self.current_start_range = []

            rule_header = self.line('@depthcontrol')
            rule_header += self.line('def {rule_name}(self):'.format(rule_name=rule_name))
            with self.indent():
                local_ctx = self.line('local_ctx = dict()')
                rule_code = self.line('current = self.create_node({node_type}(name={rule_name!r}))'.format(node_type=node_type.__name__,
                                                                                                           rule_name=rule_name))
                rule_block = node.ruleBlock() if parser_rule else node.lexerRuleBlock()
                rule_code += self.generate_single(rule_block, rule_name)
                rule_code += self.line('return current')
            rule_code += self.line('{rule_name}.min_depth = {{{rule_name}}}\n'.format(rule_name=rule_name))

            # local_ctx only has to be initialized if we have variable assignment.
            rule_code = rule_header + (local_ctx if 'local_ctx' in rule_code else '') + rule_code

            if self.labeled_alts:
                for _ in range(len(self.labeled_alts)):
                    name, children = self.labeled_alts.pop(0)
                    labeled_header = self.line('@depthcontrol')
                    labeled_header += self.line('def {name}(self):'.format(name=name))
                    with self.indent():
                        local_ctx = self.line('local_ctx = dict()')
                        labeled_code = self.line('current = self.create_node(UnparserRule(name=\'{name}\'))'.format(name=name))
                        for child in children:
                            labeled_code += self.generate_single(child, name)
                        labeled_code += self.line('return current')
                    labeled_code += self.line('{rule_name}.min_depth = {{{rule_name}}}\n'.format(rule_name=name))

                    labeled_code = labeled_header + (local_ctx if 'local_ctx' in labeled_code else '') + labeled_code
                    rule_code += labeled_code

            if not parser_rule:
                self.token_start_ranges[rule_name] = self.current_start_range
                self.current_start_range = None

            return rule_code

        if isinstance(node, (self.antlr_parser_cls.RuleAltListContext, self.antlr_parser_cls.AltListContext, self.antlr_parser_cls.LexerAltListContext)):
            children = [child for child in node.children if isinstance(child, ParserRuleContext)]
            if len(children) == 1:
                return self.generate_single(children[0], parent_id)

            alt_name = self.new_code_id('alt')
            self.graph.add_node(AlternationNode(id=alt_name))
            self.graph.add_edge(frm=parent_id, to=alt_name)

            conditions = [(self.new_code_id('cond'), self.find_conditions(child)) for child in children]
            self.code_chunks.update(conditions)
            result = self.line('choice = self.model.choice({alt_name!r}, [0 if {{{alt_name}}}[i] > self.max_depth else w for i, w in enumerate([{weights}])])'
                               .format(alt_name=alt_name,
                                       weights=', '.join('{{{cond_id}}}'.format(cond_id=cond_id) for cond_id, _ in conditions)))
            for i, child in enumerate(children):
                alternative_name = '{alt_name}_{idx}'.format(alt_name=alt_name, idx=i)
                self.graph.add_node(AlternativeNode(id=alternative_name))
                self.graph.add_edge(frm=alt_name, to=alternative_name)

                result += self.line('{if_kw} choice == {idx}:'.format(if_kw='if' if i == 0 else 'elif', idx=i))
                with self.indent():
                    result += self.generate_single(child, alternative_name) or self.line('pass')
            return result

        if isinstance(node, self.antlr_parser_cls.LabeledAltContext) and node.identifier():
            rule_name = node.parentCtx.parentCtx.parentCtx.RULE_REF().symbol.text
            name = '{rule_name}_{label_name}'.format(rule_name=rule_name,
                                                     label_name=(node.identifier().TOKEN_REF() or node.identifier().RULE_REF()).symbol.text)
            self.graph.add_node(RuleNode(id=name))
            self.graph.add_edge(frm=parent_id, to=name)
            # Notify the alternative that it's a labeled one and should be processed later.
            return self.generate_single(node.alternative(), '#' + name)

        # Sequences.
        if isinstance(node, (self.antlr_parser_cls.AlternativeContext, self.antlr_parser_cls.LexerAltContext)):
            if not node.children:
                return self.line('current += UnlexerRule(src=\'\')')

            if isinstance(node, self.antlr_parser_cls.AlternativeContext):
                children = node.element()
            elif isinstance(node, self.antlr_parser_cls.LexerAltContext):
                children = node.lexerElements().lexerElement()
            else:
                children = []

            if parent_id.startswith('#'):
                # If the current alternative is labeled then it will be processed
                # later since its content goes to a separate method.
                parent_id = parent_id[1:]
                self.labeled_alts.append((parent_id, children))
                return self.line('current = self.{name}()'.format(name=parent_id))

            return ''.join([self.generate_single(child, parent_id) for child in children])

        if isinstance(node, (self.antlr_parser_cls.ElementContext, self.antlr_parser_cls.LexerElementContext)):
            if self.actions and node.actionBlock():
                # Conditions are handled at alternative processing.
                if node.QUESTION():
                    return ''

                action_src = ''.join([str(child) for child in node.actionBlock().ACTION_CONTENT()])
                action_src = re.sub(r'\$(?P<var_name>\w+)', 'local_ctx[\'\\g<var_name>\']', action_src)

                action_id = self.new_code_id('action')
                self.code_chunks[action_id] = ''.join([self.line(line) for line in action_src.splitlines()])
                return '{{{action_id}}}'.format(action_id=action_id)

            suffix = None
            if node.ebnfSuffix():
                suffix = node.ebnfSuffix()
            elif hasattr(node, 'ebnf') and node.ebnf() and node.ebnf().blockSuffix():
                suffix = node.ebnf().blockSuffix().ebnfSuffix()

            if not suffix:
                return self.generate_single(node.children[0], parent_id)

            suffix = str(suffix.children[0])

            if suffix in ['?', '*']:
                quant_name = self.new_code_id('quant')
                self.graph.add_node(QuantifierNode(id=quant_name))
                self.graph.add_edge(frm=parent_id, to=quant_name)
                parent_id = quant_name

            quant_args = {'?': 'min=0, max=1', '*': 'min=0, max=inf', '+': 'min=1, max=inf'}[suffix]
            result = self.line('if self.max_depth >= {min_depth}:'.format(min_depth='0' if suffix == '+' else '{{{name}}}'.format(name=parent_id)))
            with self.indent():
                result += self.line('for _ in self.model.quantify({args}):'.format(args=quant_args))

                with self.indent():
                    result += self.generate_single(node.children[0], parent_id)
                result += '\n'
            return result

        if isinstance(node, self.antlr_parser_cls.LabeledElementContext):
            ident = node.identifier()
            name = ident.RULE_REF() or ident.TOKEN_REF()
            result = self.generate_single(node.atom() or node.block(), parent_id)
            result += self.line('local_ctx[\'{name}\'] = current.last_child'.format(name=name))
            return result

        if isinstance(node, self.antlr_parser_cls.RulerefContext):
            self.graph.add_edge(frm=parent_id, to=str(node.RULE_REF()))
            return self.line('current += self.{rule_name}()'.format(rule_name=node.RULE_REF()))

        if isinstance(node, (self.antlr_parser_cls.LexerAtomContext, self.antlr_parser_cls.AtomContext)):
            if node.DOT():
                return self.line('current += UnlexerRule(src=self.any_char())')

            if node.notSet():
                if node.notSet().setElement():
                    options = self.chars_from_set(node.notSet().setElement())
                else:
                    options = []
                    for set_element in node.notSet().blockSet().setElement():
                        options.extend(self.chars_from_set(set_element))

                charset_name = self.new_charset_name()
                self.generator_header += '{charset_name} = list(chain(*multirange_diff(printable_unicode_ranges, [{charset}])))\n'.format(charset_name=charset_name, charset=', '.join(['({start}, {end})'.format(start=chr_range[0], end=chr_range[1]) for chr_range in sorted(options, key=lambda x: x[0])]))
                return self.line('current += UnlexerRule(src=self.char_from_list({charset_ref}))'.format(charset_ref=charset_name))

            if isinstance(node, self.antlr_parser_cls.LexerAtomContext):
                if node.characterRange():
                    start, end = self.character_range_interval(node)
                    if self.current_start_range is not None:
                        self.current_start_range.append((start, end))
                    return self.line('current += self.create_node(UnlexerRule(src=self.char_from_list(range({start}, {end}))))'.format(start=start, end=end))

                if node.LEXER_CHAR_SET():
                    ranges = self.lexer_charset_interval(str(node.LEXER_CHAR_SET())[1:-1])

                    if self.current_start_range is not None:
                        self.current_start_range.extend(ranges)

                    charset_name = self.new_charset_name()
                    self.generator_header += '{charset_name} = list(chain({charset}))\n'.format(charset_name=charset_name, charset=', '.join(['range({start}, {end})'.format(start=chr_range[0], end=chr_range[1]) for chr_range in ranges]))
                    return self.line('current += self.create_node(UnlexerRule(src=self.char_from_list({charset_name})))'.format(charset_name=charset_name))

            return ''.join([self.generate_single(child, parent_id) for child in node.children])

        if isinstance(node, self.antlr_parser_cls.TerminalContext):
            if node.TOKEN_REF():
                self.graph.add_edge(frm=parent_id, to=str(node.TOKEN_REF()))
                return self.line('current += self.{rule_name}()'.format(rule_name=node.TOKEN_REF()))

            if node.STRING_LITERAL():
                src = str(node.STRING_LITERAL())[1:-1]
                if self.current_start_range is not None:
                    self.current_start_range.append((ord(src[0]), ord(src[0]) + 1))
                code_id = self.new_code_id('lit')
                self.code_chunks[code_id] = src
                return self.line('current += self.create_node(UnlexerRule(src=\'{{{code_id}}}\'))'.format(code_id=code_id))

        if isinstance(node, ParserRuleContext) and node.getChildCount():
            return ''.join([self.generate_single(child, parent_id) for child in node.children])

        return ''


class FuzzerFactory(object):
    """
    Class that generates fuzzers from grammars.
    """
    def __init__(self, work_dir=None, antlr=default_antlr_path):
        """
        :param work_dir: Directory to generate fuzzers into.
        :param antlr: Path to the ANTLR jar.
        """
        self.work_dir = work_dir or getcwd()

        antlr_dir = join(self.work_dir, 'antlr')
        makedirs(antlr_dir, exist_ok=True)

        # Copy the grammars from the package to the given working directory.
        antlr_resources = ['ANTLRv4Lexer.g4', 'ANTLRv4Parser.g4', 'LexBasic.g4', 'LexerAdaptor.py']
        for resource in antlr_resources:
            with open(join(antlr_dir, resource), 'wb') as f:
                f.write(get_data(__package__, join('resources', 'antlr', resource)))

        self.antlr_lexer_cls, self.antlr_parser_cls, _ = build_grammars(antlr_resources, antlr_dir, antlr=antlr)

    def generate_fuzzer(self, grammars, *, encoding='utf-8', lib_dir=None, actions=True, pep8=False):
        """
        Generates fuzzers from grammars.

        :param grammars: List of grammar files to generate from.
        :param encoding: Grammar file encoding.
        :param lib_dir: Alternative directory to look for imports.
        :param actions: Boolean to enable or disable grammar actions.
        :param pep8: Boolean to enable pep8 to beautify the generated fuzzer source.
        """
        lexer_root, parser_root = None, None

        for grammar in grammars:
            root = self._parse(grammar, encoding, lib_dir)
            # Lexer and/or combined grammars are processed first to evaluate TOKEN_REF-s.
            if root.grammarType().LEXER() or not root.grammarType().PARSER():
                lexer_root = root
            else:
                parser_root = root

        fuzzer_generator = FuzzerGenerator(self.antlr_parser_cls, actions)
        src = fuzzer_generator.generate(lexer_root, parser_root)
        with open(join(self.work_dir, fuzzer_generator.generator_name + '.py'), 'w') as f:
            if pep8:
                src = autopep8.fix_code(src)
            f.write(src)

    @staticmethod
    def _collect_imports(root, base_dir, lib_dir):
        imports = set()
        for prequel in root.prequelConstruct():
            if prequel.delegateGrammars():
                for delegate_grammar in prequel.delegateGrammars().delegateGrammar():
                    ident = delegate_grammar.identifier(0)
                    grammar_fn = str(ident.RULE_REF() or ident.TOKEN_REF()) + '.g4'
                    if lib_dir is not None and exists(join(lib_dir, grammar_fn)):
                        imports.add(join(lib_dir, grammar_fn))
                    else:
                        imports.add(join(base_dir, grammar_fn))
        return imports

    def _parse(self, grammar, encoding, lib_dir):
        work_list = {grammar}
        root = None

        while work_list:
            grammar = work_list.pop()

            antlr_parser = self.antlr_parser_cls(CommonTokenStream(self.antlr_lexer_cls(FileStream(grammar, encoding=encoding))))
            current_root = antlr_parser.grammarSpec()
            # assert antlr_parser._syntaxErrors > 0, 'Parse error in ANTLR grammar.'

            # Save the 'outermost' grammar.
            if not root:
                root = current_root
            else:
                # Unite the rules of the imported grammar with the host grammar's rules.
                for rule in current_root.rules().ruleSpec():
                    root.rules().addChild(rule)

            work_list |= self._collect_imports(current_root, dirname(grammar), lib_dir)

        return root


def execute():
    parser = ArgumentParser(description='Grammarinator: Processor', epilog="""
        The tool processes a grammar in ANTLR v4 format (*.g4, either separated
        to lexer and parser grammar files, or a single combined grammar) and
        creates a fuzzer that can generate randomized content conforming to
        the format described by the grammar.
        """)
    parser.add_argument('grammar', metavar='FILE', nargs='+',
                        help='ANTLR grammar files describing the expected format to generate.')
    parser.add_argument('--no-actions', dest='actions', default=True, action='store_false',
                        help='do not process inline actions.')
    parser.add_argument('--encoding', metavar='ENC', default='utf-8',
                        help='grammar file encoding (default: %(default)s).')
    parser.add_argument('--lib', metavar='DIR',
                        help='alternative location of import grammars.')
    parser.add_argument('--pep8', default=False, action='store_true',
                        help='enable autopep8 to format the generated fuzzer.')
    parser.add_argument('-o', '--out', metavar='DIR', default=getcwd(),
                        help='temporary working directory (default: %(default)s).')
    add_disable_cleanup_argument(parser)
    add_antlr_argument(parser)
    add_log_level_argument(parser)
    add_version_argument(parser)
    args = parser.parse_args()

    for grammar in args.grammar:
        if not exists(grammar):
            parser.error('{grammar} does not exist.'.format(grammar=grammar))

    process_log_level_argument(args)
    process_antlr_argument(args)

    FuzzerFactory(args.out, args.antlr).generate_fuzzer(args.grammar, encoding=args.encoding, lib_dir=args.lib, actions=args.actions, pep8=args.pep8)

    if args.cleanup:
        rmtree(join(args.out, 'antlr'), ignore_errors=True)


if __name__ == '__main__':
    execute()
