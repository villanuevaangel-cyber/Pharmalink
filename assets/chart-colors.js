/* One meaning per color, used by every chart.
   Teal   = sales and units that already happened
   Gold   = a forecast, not actual sales yet
   Forest = inventory cost
   Red    = loss from expiry or waste
   Doughnut slices stay inside that same family so a slice is only
   "which item", never a second status. */
(function () {
    var salesShades = ['#1E3A34', '#24544C', '#4BAA8B', '#7BC4A8', '#C5E6D8'];
    var costShades = ['#1E3A34', '#24544C', '#3D6B62', '#6B948C', '#A8C4BE'];
    var lossShades = ['#7f1d1d', '#b91c1c', '#dc2626', '#fca5a5', '#fecaca'];
    window.PharmaChart = {
        sales: '#4BAA8B',
        salesFill: 'rgba(75, 170, 139, 0.16)',
        forecast: '#FFC857',
        forecastFill: 'rgba(255, 200, 87, 0.28)',
        forecastBand: 'rgba(255, 200, 87, 0.16)',
        cost: '#1E3A34',
        loss: '#b91c1c',
        empty: '#FFE3B3',
        tooltip: '#1E3A34',
        segments: salesShades,
        slices: function (kind, count) {
            var sets = { sales: salesShades, cost: costShades, loss: lossShades, segment: salesShades };
            var base = sets[kind] || salesShades;
            var n = Math.max(0, count || 0);
            var out = [];
            for (var i = 0; i < n; i++) out.push(base[i % base.length]);
            return out;
        }
    };
})();
