"""Optional Plotly visualization for the public 2.0 model collections."""
from html import escape
import math


def plot_model(model):
    """Draw positioned nodes, link vertices, polygons and catchment outlets.

    Coordinates keep their map units; hydraulic units apply only to hydraulic
    hover values. Objects without positions are counted, never placed at zero.
    """
    import plotly.graph_objects as go
    from easysewer.model import Ref
    from easysewer.model.network import Outfall

    nodes={node.id:node for node in model.nodes.values() if node.position is not None}
    traces=[]
    centers={area.id:(sum(p.x for p in area.polygon)/len(area.polygon),
                      sum(p.y for p in area.polygon)/len(area.polygon))
             for area in model.subcatchments.values() if area.polygon}
    for area in model.subcatchments.values():
        if not area.polygon:continue
        points=(*area.polygon,area.polygon[0]) if area.polygon[-1]!=area.polygon[0] else area.polygon
        traces.append(go.Scatter(x=[p.x for p in points],y=[p.y for p in points],
            mode='lines',fill='toself',fillcolor='rgba(0,150,100,0.12)',
            line=dict(color='#009664',width=1),name=area.id,
            text='Subcatchment: '+escape(area.id),hoverinfo='text',showlegend=False))
        destination=None
        if area.outlet.collection=='swmm:nodes' and area.outlet.key in model.nodes:
            target=model.nodes[area.outlet.key]
            if target.position is not None:destination=(target.position.x,target.position.y)
        elif area.outlet.collection=='swmm:subcatchments' and area.outlet.key in model.subcatchments:
            destination=centers.get(model.subcatchments[area.outlet.key].id)
        if destination is not None:
            start=centers[area.id]
            traces.append(go.Scatter(x=[start[0],destination[0]],y=[start[1],destination[1]],
                mode='lines',line=dict(color='#53705e',width=1,dash='dot'),
                text='Runoff: '+escape(area.id)+' → '+escape(str(area.outlet.key)),
                hoverinfo='text',showlegend=False))

    skipped_links=0
    for link in model.links.values():
        upstream=model.nodes[link.inlet.key];downstream=model.nodes[link.outlet.key]
        if upstream.position is None or downstream.position is None:
            skipped_links+=1;continue
        points=(upstream.position,*link.vertices,downstream.position)
        hover='Link: '+escape(link.id)+'<br>'+escape(upstream.id)+' → '+escape(downstream.id)
        if hasattr(link,'length'):
            unit=model.inspect_field(Ref(collection='swmm:links',key=link.id),'length').semantics.unit.value
            hover+='<br>Length: '+str(link.length)+' '+escape(str(unit))
        traces.append(go.Scatter(x=[p.x for p in points],y=[p.y for p in points],
            mode='lines',line=dict(color='#2775aa',width=1.6),
            text=hover,hoverinfo='text',showlegend=False))
        lengths=[math.hypot(b.x-a.x,b.y-a.y) for a,b in zip(points,points[1:])]
        total=sum(lengths);remaining=.7*total
        if total:
            for a,b,length in zip(points,points[1:],lengths):
                if not length:continue
                if remaining>length:remaining-=length;continue
                dx,dy=(b.x-a.x)/length,(b.y-a.y)/length
                x,y=a.x+remaining*dx,a.y+remaining*dy
                size=min(total*.035,length*.2)
                traces.append(go.Scatter(x=[x,x-size*dx+.4*size*dy,x-size*dx-.4*size*dy,x],
                    y=[y,y-size*dy-.4*size*dx,y-size*dy+.4*size*dx,y],
                    mode='lines',fill='toself',fillcolor='#2775aa',line=dict(width=0),
                    text=hover,hoverinfo='text',showlegend=False))
                break
    values=list(nodes.values());elevation_unit=model.units.unit('elevation')
    traces.append(go.Scatter(x=[n.position.x for n in values],y=[n.position.y for n in values],
        mode='markers',marker=dict(size=9,color=['#c94b4b' if isinstance(n,Outfall) else '#2775aa' for n in values]),
        text=['Node: '+escape(n.id)+'<br>Elevation: '+str(n.elevation)+' '+elevation_unit for n in values],
        hoverinfo='text',name='Nodes',showlegend=False))
    figure=go.Figure(data=traces)
    figure.update_layout(title='Drainage network',template='plotly_white',height=620,
        hovermode='closest',margin=dict(l=25,r=25,t=55,b=65),
        xaxis=dict(title='Map x',showgrid=False,zeroline=False),
        yaxis=dict(title='Map y',showgrid=False,zeroline=False,scaleanchor='x',scaleratio=1))
    missing_nodes=len(model.nodes)-len(nodes)
    missing_areas=sum(not area.polygon for area in model.subcatchments.values())
    figure.add_annotation(x=0,y=-.10,xref='paper',yref='paper',xanchor='left',showarrow=False,
        text=f'Map coordinates retain their original units. Missing geometry: {missing_nodes} nodes, {skipped_links} links, {missing_areas} polygons.')
    return figure
