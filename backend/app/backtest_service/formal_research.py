"""D05 dispatch on the existing formal market reader and dependency context."""
from .formal_runtime import FormalDailyGateway, RunStoreGateway
from .research_gateway import ResearchDataGateway
from .resampling import ResampledBarGateway


class FormalResearchGateway(ResearchDataGateway, RunStoreGateway):
    pass


class FormalDailyResearchGateway(ResearchDataGateway, FormalDailyGateway):
    pass


class ResampledResearchGateway(ResearchDataGateway, ResampledBarGateway):
    pass


def with_research(kind):
    return {RunStoreGateway: FormalResearchGateway,
            FormalDailyGateway: FormalDailyResearchGateway,
            ResampledBarGateway: ResampledResearchGateway}[kind]
