export type Selection =
  | { kind: "node"; id: string }
  | { kind: "edge"; id: string };
