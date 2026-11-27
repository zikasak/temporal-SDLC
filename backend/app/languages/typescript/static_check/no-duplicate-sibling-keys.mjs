// Two children of one JSX element rendered with the same `key`. React tells siblings apart
// by key, so with a repeated one it may keep stale elements or drop live ones on the next
// render: a section shown twice, then three times, as a page re-renders. Children whose
// conditions exclude each other (`a && <X key={k} />` beside `!a && <Y key={k} />`, or the
// two branches of one ternary) are never rendered together and are not reported.

/** The text of an expression without whitespace, so formatting never decides a match. */
function text(source, node) {
  return source.getText(node).replace(/\s+/g, '');
}

/** A condition as its text and whether it is negated: `!a` is `a` negated. */
function condition(source, node, negated = false) {
  if (node.type === 'UnaryExpression' && node.operator === '!') {
    return condition(source, node.argument, !negated);
  }
  return { text: text(source, node), negated };
}

/** The operands of an `&&` chain: `a && b && c` is [a, b, c]. */
function conjuncts(node) {
  return node.type === 'LogicalExpression' && node.operator === '&&'
    ? [...conjuncts(node.left), ...conjuncts(node.right)] : [node];
}

/** The JSX elements an expression child may render, each with the conditions it needs. */
function rendered(source, node, conditions = []) {
  switch (node.type) {
    case 'JSXElement':
      return [{ element: node, conditions }];
    case 'LogicalExpression':
      if (node.operator === '&&') {
        return rendered(source, node.right,
          [...conditions, ...conjuncts(node.left).map((item) => condition(source, item))]);
      }
      return [...rendered(source, node.left, conditions),
        ...rendered(source, node.right, conditions)];
    case 'ConditionalExpression': {
      const test = condition(source, node.test);
      return [
        ...rendered(source, node.consequent, [...conditions, test]),
        ...rendered(source, node.alternate,
          [...conditions, { text: test.text, negated: !test.negated }]),
      ];
    }
    case 'TSAsExpression':
    case 'TSNonNullExpression':
    case 'TSSatisfiesExpression':
      return rendered(source, node.expression, conditions);
    default:
      return [];
  }
}

/** The element's key as comparable text, or null without one. */
function keyOf(source, element) {
  const attribute = element.openingElement.attributes.find(
    (item) => item.type === 'JSXAttribute' && item.name.name === 'key');
  if (!attribute || !attribute.value) return null;
  let value = attribute.value;
  if (value.type === 'JSXExpressionContainer') value = value.expression;
  if (value.type === 'Literal') return { attribute, text: JSON.stringify(String(value.value)) };
  if (value.type === 'TemplateLiteral' && value.expressions.length === 0) {
    return { attribute, text: JSON.stringify(value.quasis[0].value.cooked) };
  }
  return { attribute, text: text(source, value) };
}

function exclusive(first, second) {
  return first.some((a) => second.some((b) => a.text === b.text && a.negated !== b.negated));
}

export default {
  meta: {
    type: 'problem',
    docs: { description: 'Disallow two sibling JSX children with the same key' },
    schema: [],
    messages: {
      duplicate: 'Another child of this element (line {{line}}) has the same key {{key}}: '
        + 'React matches siblings by key, so it may keep a stale copy or drop one on '
        + 'the next render. Give each sibling a key of its own.',
    },
  },
  create(context) {
    const source = context.sourceCode ?? context.getSourceCode();
    function check(node) {
      const seen = [];
      node.children.forEach((child, index) => {
        const candidates = child.type === 'JSXElement' ? [{ element: child, conditions: [] }]
          : child.type === 'JSXExpressionContainer' ? rendered(source, child.expression) : [];
        for (const { element, conditions } of candidates) {
          const key = keyOf(source, element);
          if (!key) continue;
          // The alternatives of one child are never rendered together.
          const earlier = seen.find((item) => item.index !== index && item.key.text === key.text
            && !exclusive(item.conditions, conditions));
          if (earlier) {
            context.report({
              node: key.attribute, messageId: 'duplicate',
              data: { key: key.text, line: earlier.key.attribute.loc.start.line },
            });
          }
          seen.push({ index, key, conditions });
        }
      });
    }
    return { JSXElement: check, JSXFragment: check };
  },
};
