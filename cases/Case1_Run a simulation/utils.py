import easysewer
import plotly.graph_objects as go
import math


def plot_model(model):
    """
    Creates a network visualization of a model using Plotly without NetworkX dependency.
    Displays nodes, links, area polygons, and connections from areas to outlet nodes.
    Links include directional arrows to show flow direction.

    Args:
        model: The input model containing nodes, links, and areas.

    Returns:
        plotly.graph_objects.Figure: The figure object for the graph visualization.
    """

    # Create dictionaries to store node positions and attributes
    nodes = {}

    # Process nodes from the model
    for node in model.node:
        name = node.name
        x, y = node.coordinate
        is_outfall = isinstance(node, easysewer.Node.Outfall)  # Using the fully qualified class name
        nodes[name] = {'x': x, 'y': y, 'is_outfall': is_outfall}

    # Process links
    links = []
    for link in model.link:
        upstream = link.upstream_node
        downstream = link.downstream_node

        links.append({
            'upstream': upstream,
            'downstream': downstream,
            'length': link.length,
            'name': link.name
        })

    # Create edge trace with arrows
    edge_traces = []

    for link in links:
        upstream = link['upstream']
        downstream = link['downstream']

        if upstream in nodes and downstream in nodes:
            x0, y0 = nodes[upstream]['x'], nodes[upstream]['y']
            x1, y1 = nodes[downstream]['x'], nodes[downstream]['y']

            # Calculate arrow positions - arrow at 70% of the way from start to end
            arrow_ratio = 0.7
            arrow_x = x0 + (x1 - x0) * arrow_ratio
            arrow_y = y0 + (y1 - y0) * arrow_ratio

            # Create hover text
            edge_info = [
                f"Link: {link['name']}",
                f"From: {upstream} To: {downstream}",
                f"Length: {link['length']:.2f}m",
            ]
            hover_text = "<br>".join(edge_info)

            # Create the edge trace
            edge_trace = go.Scatter(
                x=[x0, x1],
                y=[y0, y1],
                line=dict(width=1.0, color='#1f77b4'),
                hoverinfo='text',
                text=hover_text,
                mode='lines',
                showlegend=False
            )

            # Calculate the direction vector
            dx = x1 - x0
            dy = y1 - y0

            # Normalize the vector to get a unit vector
            length = math.sqrt(dx**2 + dy**2)
            if length > 0:
                dx, dy = dx/length, dy/length

            # Create a perpendicular vector for the arrow wings
            wing_size = 80  # Increased size of the arrow wings
            perp_dx, perp_dy = -dy, dx

            # Create the arrow head - a small filled triangle
            # Start with the arrow tip
            arrow_x = [arrow_x]
            arrow_y = [arrow_y]

            # Add the two wing points to form a triangle
            arrow_x.extend([arrow_x[0] - wing_size*dx + wing_size*0.4*perp_dx,
                           arrow_x[0] - wing_size*dx - wing_size*0.4*perp_dx])
            arrow_y.extend([arrow_y[0] - wing_size*dy + wing_size*0.4*perp_dy,
                           arrow_y[0] - wing_size*dy - wing_size*0.4*perp_dy])

            # Create arrow marker as a filled polygon with invisible vertices
            arrow_trace = go.Scatter(
                x=arrow_x,
                y=arrow_y,
                mode='lines',  # Changed from 'lines+markers' to just 'lines'
                fill='toself',
                fillcolor='#1f77b4',
                line=dict(color='#1f77b4', width=0),  # Set line width to 0 to hide the outline
                hoverinfo='text',
                text=hover_text,
                showlegend=False
            )

            edge_traces.append(edge_trace)
            edge_traces.append(arrow_trace)

    # Create node trace
    node_x, node_y, node_text, node_colors = [], [], [], []

    for name, attrs in nodes.items():
        x, y = attrs['x'], attrs['y']
        node_x.append(x)
        node_y.append(y)

        hover_text = f"Node: {name}<br>" + "<br>".join([
            f"{k}: {v:.2f}" if isinstance(v, float) else f"{k}: {v}"
            for k, v in attrs.items()
        ])
        node_text.append(hover_text)

        # Outfall nodes are colored red, others are blue
        if attrs['is_outfall']:
            node_colors.append('red')
        else:
            node_colors.append('blue')

    node_trace = go.Scatter(
        x=node_x, y=node_y,
        mode='markers',
        hoverinfo='text',
        text=node_text,
        marker=dict(
            size=10,
            color=node_colors,
            line_width=2))

    # Create area polygon traces and area-to-outlet connections
    area_traces = []
    area_connections_x = []
    area_connections_y = []
    area_connections_text = []

    for i, area in enumerate(model.area):
        x_coords = area.polygon.x
        y_coords = area.polygon.y

        # Calculate polygon center (average of coordinates)
        center_x = sum(x_coords) / len(x_coords) if x_coords else 0
        center_y = sum(y_coords) / len(y_coords) if y_coords else 0

        # Close the polygon by adding the first point at the end
        if len(x_coords) > 0 and (x_coords[0] != x_coords[-1] or y_coords[0] != y_coords[-1]):
            x_coords = list(x_coords) + [x_coords[0]]
            y_coords = list(y_coords) + [y_coords[0]]

        # Create a trace for the area
        area_name = getattr(area, 'name', f'Area {i+1}')
        area_trace = go.Scatter(
            x=x_coords,
            y=y_coords,
            fill="toself",
            fillcolor=f'rgba(0, 255, 127, 0.2)',  # Light green with transparency
            line=dict(color='green', width=1),
            hoverinfo='text',
            text=f"Area: {area_name}",
            mode='lines',
            showlegend=False
        )

        area_traces.append(area_trace)

        # Add dotted line from polygon center to its outlet node
        outlet = area.outlet
        if outlet in nodes:
            outlet_x = nodes[outlet]['x']
            outlet_y = nodes[outlet]['y']

            # Add connection line (dotted)
            area_connections_x.extend([center_x, outlet_x, None])
            area_connections_y.extend([center_y, outlet_y, None])

            connection_text = f"Area: {area_name}<br>Outlet: {outlet}"
            area_connections_text.extend([connection_text, connection_text, None])

    # Create a trace for the area-to-outlet connections
    area_connections_trace = go.Scatter(
        x=area_connections_x,
        y=area_connections_y,
        line=dict(width=0.7, color='darkgreen', dash='dash'),
        hoverinfo='text',
        text=area_connections_text,
        mode='lines',
        name='Area Connections'
    )

    # Create and return the figure with all traces
    # Order: area polygons (background), area connections, links, nodes (foreground)
    fig = go.Figure(
        data=[*area_traces, area_connections_trace, *edge_traces, node_trace],
        layout=go.Layout(
            title='Network Visualization',
            showlegend=False,
            hovermode='closest',
            margin=dict(b=20, l=5, r=5, t=40),
            xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
            yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
            # Set the aspect ratio to be equal
            yaxis_scaleanchor="x",
            yaxis_scaleratio=1,
        ))

    return fig
