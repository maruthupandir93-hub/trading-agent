from typing import Dict, List, Any
import networkx as nx
import logging

logger = logging.getLogger(__name__)

class KnowledgeGraph:
    """
    Level 4: Knowledge Graph
    Instead of simple memory, the AI should understand relationships.
    e.g., High Funding -> High Liq Risk -> Lower Position Size
    """
    def __init__(self):
        self.graph = nx.DiGraph()
        self._initialize_base_rules()

    def _initialize_base_rules(self):
        # Base trading logic rules stored as graph relationships
        self.add_relationship("High Funding", "High Liquidation Risk", weight=0.8)
        self.add_relationship("High Liquidation Risk", "Lower Position Size", weight=1.0)
        
        # THE SECTION 21 VOCABULARY, which is what every caller passes (F92).
        #
        # These were keyed "Trending Bullish" and "Ranging" while the regime
        # classifier returns "Bull Trend" and "Range", so
        # `/api/knowledge/implications/Bull Trend` returned 404 and the graph
        # answered nothing for any real regime. Both spellings are registered:
        # the canonical names so live queries work, and the profile names so an
        # existing caller that learned the old keys is not broken.
        for bullish in ("Bull Trend", "Trending Bullish"):
            self.add_relationship(bullish, "Trend Following Strategy", weight=0.9)
        for bearish in ("Bear Trend", "Trending Bearish"):
            self.add_relationship(bearish, "Trend Following Strategy", weight=0.9)
        for ranging in ("Range", "Ranging", "Low Volatility"):
            self.add_relationship(ranging, "Mean Reversion Strategy", weight=0.9)
        
    def add_relationship(self, source: str, target: str, weight: float = 1.0):
        self.graph.add_edge(source, target, weight=weight)
        
    def query_implications(self, state: str) -> List[str]:
        """Query what a certain market state implies."""
        if state not in self.graph:
            return []
            
        implications = []
        # Get immediate neighbors
        for neighbor in self.graph.successors(state):
            implications.append(neighbor)
            # Get 2nd degree implications
            for secondary in self.graph.successors(neighbor):
                if secondary not in implications:
                    implications.append(secondary)
                    
        return implications

_kg = KnowledgeGraph()

def get_knowledge_graph() -> KnowledgeGraph:
    return _kg
