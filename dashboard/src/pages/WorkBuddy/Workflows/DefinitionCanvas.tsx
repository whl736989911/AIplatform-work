/**
 * The workflow as a graph (A-16).
 *
 * A second view of the same definition, not a second editor: it draws what the
 * JSON says, and the only structural change it makes is **connections**. Node
 * creation stays with the wizard (a step needs a type and the schema's required
 * fields, which is the metadata-driven form's job), and moving a box is a view
 * concern kept in the per-workflow layout store.
 *
 * That split is what makes the canvas safe to hand to a non-technical user: a
 * drag can rearrange the picture, but only a connect or a disconnect can change
 * what the workflow actually does — and every such change is reported upwards as
 * a new definition, from the handler that caused it rather than from an effect
 * watching state (which is how a canvas and its JSON start echoing each other).
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Background,
  Controls,
  ReactFlow,
  addEdge,
  type Connection,
  type Edge,
  type Node,
  type NodeChange,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import {
  readLayout,
  toCanvas,
  withCanvasEdges,
  writeLayout,
  type CanvasLayout,
  type GraphDefinition,
} from "./definitionGraph";

export default function DefinitionCanvas({
  definition,
  layoutKey,
  onChange,
}: {
  definition: GraphDefinition;
  /** Identifies which workflow's layout to keep: a view concern, never the JSON. */
  layoutKey: string;
  /** Called with a new definition when the connections change. */
  onChange: (next: GraphDefinition) => void;
}) {
  const [layout, setLayout] = useState<CanvasLayout>(() => readLayout(layoutKey));
  const graph = useMemo(() => toCanvas(definition, layout), [definition, layout]);
  const [nodes, setNodes] = useState<Node[]>(graph.nodes);
  const [edges, setEdges] = useState<Edge[]>(graph.edges);

  // The definition is the source of truth: when it changes (an edit elsewhere,
  // a reloaded version) the picture follows it rather than drifting.
  useEffect(() => {
    setNodes(graph.nodes);
    setEdges(graph.edges);
  }, [graph]);

  const onNodesChange = useCallback(
    (changes: NodeChange[]) => {
      let moved = false;
      const next = { ...layout };
      for (const change of changes) {
        if (change.type === "position" && change.position) {
          next[change.id] = { x: change.position.x, y: change.position.y };
          moved = true;
        }
      }
      if (!moved) return;
      setLayout(next);
      writeLayout(layoutKey, next);
      // Note what is *not* here: no definition change. Where a box sits is not
      // part of what the workflow does.
      setNodes((current) =>
        current.map((node) =>
          next[node.id] ? { ...node, position: next[node.id] } : node,
        ),
      );
    },
    [layout, layoutKey],
  );

  const connect = useCallback(
    (connection: Connection) => {
      const id = `${connection.source}->${connection.target}`;
      const next = addEdge({ ...connection, id }, edges);
      setEdges(next);
      onChange(withCanvasEdges(definition, next));
    },
    [definition, edges, onChange],
  );

  const removeEdges = useCallback(
    (removed: Edge[]) => {
      if (removed.length === 0) return;
      const gone = new Set(removed.map((edge) => edge.id));
      const next = edges.filter((edge) => !gone.has(edge.id));
      setEdges(next);
      onChange(withCanvasEdges(definition, next));
    },
    [definition, edges, onChange],
  );

  return (
    <div style={{ height: 420, width: "100%" }} data-testid="workbuddy-definition-canvas">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onConnect={connect}
        onEdgesDelete={removeEdges}
        fitView
        proOptions={{ hideAttribution: true }}
      >
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
