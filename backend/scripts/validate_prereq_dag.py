import os

import networkx as nx
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()


def get_client():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY"))


def fetch_all_edges(client) -> list[dict]:
    edges = []
    page_size = 1000
    offset = 0
    while True:
        rows = (
            client.table("prereq_edges")
            .select("course_id,prereq_course_id")
            .range(offset, offset + page_size - 1)
            .execute()
            .data
        )
        edges.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size
    return edges


def main() -> None:
    client = get_client()

    print("Fetching prereq_edges...")
    edges = fetch_all_edges(client)
    print(f"  {len(edges)} edges loaded.\n")

    G = nx.DiGraph()
    for row in edges:
        G.add_edge(row["prereq_course_id"], row["course_id"])

    print(f"Graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")

    if nx.is_directed_acyclic_graph(G):
        print("DAG is valid - no cycles found")
    else:
        cycle = nx.find_cycle(G)
        print(f"CYCLE DETECTED ({len(cycle)} edges):")
        for src, dst in cycle:
            print(f"  {src} -> {dst}")


if __name__ == "__main__":
    main()
