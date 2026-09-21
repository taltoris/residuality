// React component for interactive directory graph explorer
// Assumes React is loaded globally and Babel will transform JSX in the browser.

const { useState, useEffect, useMemo } = React;

function GraphExplorer({ projectId }) {
  const [nodes, setNodes] = useState([]);
  const [currentPath, setCurrentPath] = useState([]);
  const [modal, setModal] = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch(`/projects/${projectId}/graph/data`)
      .then(r => r.json())
      .then(data => {
        setNodes(data.nodes);
        setLoading(false);
      })
      .catch(e => console.error('graph data error', e));
  }, [projectId]);

  // Filter nodes for current directory view
  const visible = useMemo(() => {
    const prefix = currentPath.join('/') + (currentPath.length ? '/' : '');
    return nodes.filter(n => {
      if (!n.file) return currentPath.length === 0; // root nodes like README
      return n.file.startsWith(prefix);
    });
  }, [nodes, currentPath]);

  // Build a tree of directories -> files -> nodes
  const tree = useMemo(() => {
    const t = {};
    visible.forEach(n => {
      if (!n.file) return;
      const parts = n.file.split('/');
      const dir = parts.slice(0, -1).join('/') || 'root';
      const file = parts[parts.length - 1];
      if (!t[dir]) t[dir] = {};
      if (!t[dir][file]) t[dir][file] = [];
      t[dir][file].push(n);
    });
    return t;
  }, [visible]);

  const goInto = dir => {
    if (dir === 'root') setCurrentPath([]);
    else setCurrentPath([...currentPath, dir]);
  };
  const goBack = () => setCurrentPath(currentPath.slice(0, -1));

  const openNode = node => {
    // Simple placeholder: just show node id and signature
    setModal({ title: node.id, body: node.signature || node.type });
  };

  if (loading) return React.createElement('div', {className: 'card'}, 'Loading graph...');

  return React.createElement('div', {className: 'graph-explorer'},
    // Breadcrumb
    React.createElement('div', {className: 'breadcrumb', style: {marginBottom: '1rem'}},
      React.createElement('button', {className: 'btn', onClick: goBack}, 'root'),
      currentPath.map((seg, i) =>
        React.createElement(React.Fragment, {key: i},
          React.createElement('span', null, ' / '),
          React.createElement('button', {className: 'btn', onClick: () => setCurrentPath(currentPath.slice(0, i+1))}, seg)
        )
      )
    ),
    // Main columns
    React.createElement('div', {style: {display: 'flex', gap: '2rem'}},
      // Directories column
      React.createElement('div', {style: {flex: 1}},
        React.createElement('h3', null, 'Directories'),
        Object.keys(tree).filter(d => d !== currentPath.join('/') && d !== 'root').map(d =>
          React.createElement('div', {key: d, className: 'tree-item dir', style: {cursor: 'pointer', color: '#58a6ff'}, onClick: () => goInto(d)}, '📁 ', d)
        )
      ),
      // Files column
      React.createElement('div', {style: {flex: 2}},
        React.createElement('h3', null, 'Files & Structure'),
        (tree[currentPath.join('/') ] || {}).map ? Object.entries(tree[currentPath.join('/') ] || {}).map(([file, nodes]) =>
          React.createElement('div', {key: file, style: {marginBottom: '1rem'}},
            React.createElement('div', {style: {fontWeight: 'bold'}}, '📄 ', file),
            nodes.map(n =>
              React.createElement('div', {key: n.id, style: {display: 'flex', justifyContent: 'space-between', alignItems: 'center'}},
                React.createElement('span', null, n.signature || n.type),
                React.createElement('button', {className: 'btn btn-sm', onClick: () => openNode(n)}, '👁️')
              )
            )
          )
        ) : null
      )
    ),
    // Modal overlay
    modal && React.createElement('div', {className: 'modal-overlay', onClick: () => setModal(null), style: {position: 'fixed', top:0, left:0, width:'100%', height:'100%', background:'rgba(0,0,0,0.6)', display:'flex', alignItems:'center', justifyContent:'center'}},
      React.createElement('div', {className: 'modal-content', onClick: e=>e.stopPropagation(), style: {background:'#0d1117', padding:'1rem', borderRadius:'6px', maxWidth:'80%'}},
        React.createElement('h2', null, modal.title),
        React.createElement('pre', null, modal.body),
        React.createElement('button', {className: 'btn', onClick: () => setModal(null)}, 'Close')
      )
    )
  );
}

// Expose globally for inclusion in the page
window.GraphExplorer = GraphExplorer;
