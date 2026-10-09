// Raw qbXML in README samples must be what the API's XML passthrough accepts, or a reader who
// copies the sample gets 400 PASSTHROUGH_INVALID_QBXML (the Python and .NET samples once sent a bare
// <CustomerQueryRq>). Used by readme-samples.mjs; mirrors the API's rule for `Content-Type: */xml`:
//
// - an optional <?xml ...?> and <?qbxml ...?> prolog, then either <QBXMLMsgsRq> or
//   <QBXML><QBXMLMsgsRq>...</QBXMLMsgsRq></QBXML>;
// - well formed, with at least one request element inside <QBXMLMsgsRq>;
// - onError, when present, is stopOnError or continueOnError; newMessageSetID and oldMessageSetID
//   are set by the API, never by the caller.
//
// No dependencies beyond Node.js 18+.

/** String literals in source code: "..." '...' `...` (no interpolation) and C# @"...". */
const LITERAL = /@"(?:""|[^"])*"|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\$])*`/g;

function unquote(lit) {
  if (lit.startsWith('@"')) return lit.slice(2, -1).replace(/""/g, '"');
  return lit.slice(1, -1).replace(/\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)/g, (_, e) => {
    if (e[0] === "u" || e[0] === "x") return String.fromCharCode(parseInt(e.slice(1), 16));
    return { n: "\n", r: "\r", t: "\t" }[e] ?? e;
  });
}

/**
 * The qbXML request strings in one code sample: adjacent string literals joined by `+`, whitespace
 * or nothing (Python's implicit concatenation) form one string; strings that start with `<` and
 * name a request element (`...Rq>`) or a QBXML envelope are returned.
 */
export function qbxmlStrings(code) {
  const lits = [...code.matchAll(LITERAL)].map((m) => ({ start: m.index, end: m.index + m[0].length, text: unquote(m[0]) }));
  const groups = [];
  for (const l of lits) {
    const prev = groups.at(-1);
    if (prev && /^[\s+]*$/.test(code.slice(prev.end, l.start))) {
      prev.text += l.text;
      prev.end = l.end;
    } else groups.push({ ...l });
  }
  return groups.map((g) => g.text.trim()).filter((t) => t.startsWith("<") && (/Rq[\s/>]/.test(t) || /<QBXML[\s>]/.test(t)));
}

/** Parses XML into { name, attrs, children } elements; throws on anything not well formed. */
function parse(xml) {
  const root = { name: "#document", attrs: {}, children: [] };
  const stack = [root];
  const re = /<\?[\s\S]*?\?>|<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>|<\/([\w:.-]+)\s*>|<([\w:.-]+)((?:\s+[\w:.-]+\s*=\s*(?:"[^"]*"|'[^']*'))*)\s*(\/?)>|([^<]+)|(<)/g;
  let m;
  while ((m = re.exec(xml))) {
    const [all, close, open, attrText, selfClose, text, stray] = m;
    if (stray) throw new Error(`unexpected "<" at offset ${m.index}`);
    if (text !== undefined) {
      if (stack.length === 1 && text.trim()) throw new Error("text outside the root element");
      continue;
    }
    if (all.startsWith("<?") || all.startsWith("<!--")) continue;
    if (all.startsWith("<![CDATA[")) {
      if (stack.length === 1) throw new Error("CDATA outside the root element");
      continue;
    }
    if (close) {
      const top = stack.pop();
      if (!top || top === root || top.name !== close) throw new Error(`</${close}> does not close <${top?.name ?? "nothing"}>`);
      continue;
    }
    const attrs = {};
    for (const a of attrText.matchAll(/([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/g)) attrs[a[1]] = a[2] ?? a[3];
    const el = { name: open, attrs, children: [] };
    stack.at(-1).children.push(el);
    if (stack.length === 1 && root.children.length > 1) throw new Error("more than one root element");
    if (!selfClose) stack.push(el);
  }
  if (stack.length !== 1) throw new Error(`<${stack.at(-1).name}> is not closed`);
  if (root.children.length !== 1) throw new Error("no root element");
  return root.children[0];
}

/** Why the API would reject this passthrough XML body, or null when it accepts it. */
export function passthroughXmlProblem(xml) {
  let root;
  try {
    root = parse(xml.trim());
  } catch (e) {
    return `not well formed: ${e.message}`;
  }
  const set = root.name === "QBXMLMsgsRq" ? root : root.name === "QBXML" ? root.children.find((c) => c.name === "QBXMLMsgsRq") : undefined;
  if (!set) return `the root is <${root.name}>; send a <QBXMLMsgsRq> fragment (or <QBXML><QBXMLMsgsRq>), for example <QBXMLMsgsRq onError="stopOnError"><CustomerQueryRq/></QBXMLMsgsRq>`;
  if (!set.children.length) return "<QBXMLMsgsRq> contains no request elements";
  if (set.attrs.onError !== undefined && set.attrs.onError !== "stopOnError" && set.attrs.onError !== "continueOnError") return "onError must be stopOnError or continueOnError";
  if (set.attrs.newMessageSetID !== undefined || set.attrs.oldMessageSetID !== undefined) return "newMessageSetID and oldMessageSetID are set by the API";
  return null;
}

/** Problems in one sample's code: [{ xml, problem }]. */
export function checkQbxmlSamples(code) {
  return qbxmlStrings(code).flatMap((xml) => {
    const problem = passthroughXmlProblem(xml);
    return problem ? [{ xml, problem }] : [];
  });
}
