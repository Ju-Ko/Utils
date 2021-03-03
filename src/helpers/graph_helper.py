import pandas

from io import BytesIO


def file_from_timestamps(times, group):
    print("process spawned.")
    file = BytesIO()
    print("making series")
    series = pandas.Series(times)
    series.index = series.dt.to_period(group)
    series = series.groupby(level=0).size()
    series = series.reindex(pandas.period_range(series.index.min(), series.index.max(), freq=group), fill_value=0)
    print("plotting graph")
    bar_chart = series.plot.bar(subplots=False)
    figure = bar_chart.get_figure()
    figure.tight_layout()
    print("saving figure")
    figure.savefig(file)
    file.seek(0)
    print("done")
    return file
