from .context import PipelineContext

class QuantPipeline:
    def __init__(self):
        self.stages = []

    def add(self, stage):
        self.stages.append(stage)
        return self

    def run(self, data=None, context=None):
        context = context or PipelineContext()
        for stage in self.stages:
            data = stage.run(data, context)
        return data
