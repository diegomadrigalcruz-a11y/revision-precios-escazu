from flask import Flask, render_template, request, jsonify
import warnings
warnings.filterwarnings('ignore')
from bccr import SW
import pandas as pd
from datetime import datetime, date
import traceback

app = Flask(__name__)

INDICES = {
    'ismn':  {'codigo': 1076,  'nombre': 'Índice de Salarios Mínimos Nominales (ISMN, 1984=100)'},
    'ipc':   {'codigo': 25482, 'nombre': 'IPC (junio 2015=100)'},
    'ipc20': {'codigo': 89635, 'nombre': 'IPC (diciembre 2020=100)'},
    'ipp':   {'codigo': 42091, 'nombre': 'IPP-S Manufactura'},
}


def fecha_bccr(fecha_str: str) -> str:
    """Convierte YYYY-MM a dd/mm/yyyy (primer día del mes)."""
    dt = datetime.strptime(fecha_str, '%Y-%m')
    return dt.strftime('01/%m/%Y')


def obtener_valor_indice(codigo: int, periodo: str) -> tuple[float, str] | tuple[None, None]:
    """
    Retorna (valor, periodo_real). Si el período exacto no tiene dato (p.ej. ISMN
    solo publica en enero y julio), usa el valor publicado más reciente anterior al período.
    Busca hasta 12 meses atrás.
    """
    dt = datetime.strptime(periodo, '%Y-%m')
    anio_i, mes_i = dt.year, dt.month - 12
    while mes_i <= 0:
        mes_i += 12
        anio_i -= 1
    anio_f, mes_f = dt.year, dt.month + 2
    while mes_f > 12:
        mes_f -= 12
        anio_f += 1
    inicio = f'01/{mes_i:02d}/{anio_i}'
    fin    = f'01/{mes_f:02d}/{anio_f}'
    try:
        df = SW.datos(codigo, FechaInicio=inicio, FechaFinal=fin)
        if df.empty:
            return None, None
        df.index = pd.to_datetime(df.index.astype(str))
        target = pd.Timestamp(dt)
        disponibles = df[df.index <= target]
        if disponibles.empty:
            return None, None
        fila = disponibles.iloc[-1]
        periodo_real = disponibles.index[-1].strftime('%Y-%m')
        return float(fila.iloc[0]), periodo_real
    except Exception:
        return None, None


def ultimos_periodos(codigo: int, n: int = 12) -> list[dict]:
    """Retorna los últimos n períodos disponibles para un indicador."""
    try:
        df = SW.datos(codigo, FechaInicio='01/01/2023', FechaFinal='25/04/2026')
        if df.empty:
            return []
        df.index = pd.to_datetime(df.index.astype(str))
        df = df.sort_index()
        resultado = []
        for idx in df.index[-n:]:
            periodo = idx.strftime('%Y-%m')
            valor = float(df.loc[idx].iloc[0])
            resultado.append({'periodo': periodo, 'valor': round(valor, 6)})
        return resultado
    except Exception:
        return []


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/indices', methods=['GET'])
def api_indices():
    """Lista de índices disponibles."""
    return jsonify([
        {'clave': k, 'nombre': v['nombre'], 'codigo': v['codigo']}
        for k, v in INDICES.items()
    ])


@app.route('/api/periodos/<indice>', methods=['GET'])
def api_periodos(indice: str):
    """Retorna los últimos períodos disponibles para un índice."""
    if indice not in INDICES:
        return jsonify({'error': 'Índice no encontrado'}), 404
    codigo = INDICES[indice]['codigo']
    periodos = ultimos_periodos(codigo, 24)
    return jsonify({'indice': indice, 'periodos': periodos})


@app.route('/api/calcular', methods=['POST'])
def api_calcular():
    """
    Cuerpo esperado:
    {
        "precio_cotizacion": 150000,
        "mo": 0.45,   // porcentaje como decimal
        "insumos": 0.20,
        "ga": 0.25,
        "utilidad": 0.10,
        "indice_mo": "ismn",
        "periodo_cotizacion": "2024-01",
        "periodo_variacion": "2026-01"
    }
    """
    try:
        data = request.get_json()
        pc     = float(data['precio_cotizacion'])
        mo     = float(data['mo'])
        ins    = float(data['insumos'])
        ga     = float(data['ga'])
        util   = float(data['utilidad'] or 0)
        p_cot  = data['periodo_cotizacion']
        p_var  = data['periodo_variacion']
        ind_mo = data.get('indice_mo', 'ismn')

        if abs(mo + ins + ga + util - 1.0) > 0.0001:
            return jsonify({'error': 'Los porcentajes Mo + I + GA + U deben sumar 100%'}), 400

        cod_mo  = INDICES[ind_mo]['codigo']
        cod_ins = INDICES['ipp']['codigo']
        cod_ga  = INDICES['ipc']['codigo']

        iMOtc, rMOtc = obtener_valor_indice(cod_mo, p_cot)
        iMOtm, rMOtm = obtener_valor_indice(cod_mo, p_var)
        iltc,  rltc  = obtener_valor_indice(cod_ins, p_cot)
        ilti,  rlti  = obtener_valor_indice(cod_ins, p_var)
        iGAtc, rGAtc = obtener_valor_indice(cod_ga, p_cot)
        iGAtg, rGAtg = obtener_valor_indice(cod_ga, p_var)

        errores = []
        if iMOtc is None: errores.append(f'Sin dato de mano de obra para {p_cot} (ni en los 12 meses anteriores)')
        if iMOtm is None: errores.append(f'Sin dato de mano de obra para {p_var} (ni en los 12 meses anteriores)')
        if iltc  is None: errores.append(f'Sin dato de insumos para {p_cot}')
        if ilti  is None: errores.append(f'Sin dato de insumos para {p_var}')
        if iGAtc is None: errores.append(f'Sin dato de gastos adm. para {p_cot}')
        if iGAtg is None: errores.append(f'Sin dato de gastos adm. para {p_var}')
        if errores:
            return jsonify({'error': '; '.join(errores)}), 422

        factor_mo  = mo  * (iMOtm / iMOtc)
        factor_ins = ins * (ilti  / iltc)
        factor_ga  = ga  * (iGAtg / iGAtc)
        factor_u   = util

        suma_factores = factor_mo + factor_ins + factor_ga + factor_u
        pv = pc * suma_factores

        variacion_abs = pv - pc
        variacion_pct = (variacion_abs / pc) * 100
        procede       = abs(variacion_pct) >= 5.0

        return jsonify({
            'precio_cotizacion': pc,
            'precio_variado': round(pv, 2),
            'variacion_absoluta': round(variacion_abs, 2),
            'variacion_porcentual': round(variacion_pct, 4),
            'procede': procede,
            'indices': {
                'mano_de_obra': {
                    'nombre': INDICES[ind_mo]['nombre'],
                    'cotizacion': {'periodo': rMOtc, 'valor': round(iMOtc, 6)},
                    'variacion':  {'periodo': rMOtm, 'valor': round(iMOtm, 6)},
                    'ratio': round(iMOtm / iMOtc, 6),
                },
                'insumos': {
                    'nombre': INDICES['ipp']['nombre'],
                    'cotizacion': {'periodo': rltc, 'valor': round(iltc, 6)},
                    'variacion':  {'periodo': rlti, 'valor': round(ilti, 6)},
                    'ratio': round(ilti / iltc, 6),
                },
                'gastos_administrativos': {
                    'nombre': INDICES['ipc']['nombre'],
                    'cotizacion': {'periodo': rGAtc, 'valor': round(iGAtc, 6)},
                    'variacion':  {'periodo': rGAtg, 'valor': round(iGAtg, 6)},
                    'ratio': round(iGAtg / iGAtc, 6),
                },
            },
            'factores': {
                'mo':  round(factor_mo, 6),
                'ins': round(factor_ins, 6),
                'ga':  round(factor_ga, 6),
                'u':   round(factor_u, 6),
                'suma': round(suma_factores, 6),
            },
        })

    except KeyError as e:
        return jsonify({'error': f'Campo requerido faltante: {e}'}), 400
    except Exception:
        return jsonify({'error': traceback.format_exc()}), 500


if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5050))
    app.run(debug=False, host='0.0.0.0', port=port)
