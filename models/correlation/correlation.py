import cupy
import torch
import re

kernel_Correlation_rearrange = '''
	extern "C" __global__ void kernel_Correlation_rearrange(
		const int n,
		const float* input,
		float* output
	) {
	  int intIndex = (blockIdx.x * blockDim.x) + threadIdx.x;

	  if (intIndex >= n) {
	    return;
	  }

	  int intSample = blockIdx.z;
	  int intChannel = blockIdx.y;

	  float dblValue = input[(((intSample * SIZE_1(input)) + intChannel) * SIZE_2(input) * SIZE_3(input)) + intIndex];

	  __syncthreads();

	  int intPaddedY = (intIndex / SIZE_3(input)) + 4;
	  int intPaddedX = (intIndex % SIZE_3(input)) + 4;
	  int intRearrange = ((SIZE_3(input) + 8) * intPaddedY) + intPaddedX;

	  output[(((intSample * SIZE_1(output) * SIZE_2(output)) + intRearrange) * SIZE_1(input)) + intChannel] = dblValue;
	}
'''

kernel_Correlation_updateOutput = '''
	extern "C" __global__ void kernel_Correlation_updateOutput(
	  const int n,
	  const float* rbot0,
	  const float* rbot1,
	  float* top
	) {
	  extern __shared__ char patch_data_char[];
	  
	  float *patch_data = (float *)patch_data_char;
	  
	  // First (upper left) position of kernel upper-left corner in current center position of neighborhood in image 1
	  int x1 = blockIdx.x + 4;
	  int y1 = blockIdx.y + 4;
	  int item = blockIdx.z;
	  int ch_off = threadIdx.x;
	  
	  // Load 3D patch into shared shared memory
	  for (int j = 0; j < 1; j++) { // HEIGHT
	    for (int i = 0; i < 1; i++) { // WIDTH
	      int ji_off = ((j * 1) + i) * SIZE_3(rbot0);
	      for (int ch = ch_off; ch < SIZE_3(rbot0); ch += 32) { // CHANNELS
	        int idx1 = ((item * SIZE_1(rbot0) + y1+j) * SIZE_2(rbot0) + x1+i) * SIZE_3(rbot0) + ch;
	        int idxPatchData = ji_off + ch;
	        patch_data[idxPatchData] = rbot0[idx1];
	      }
	    }
	  }
	  
	  __syncthreads();
	  
	  __shared__ float sum[32];
	  
	  // Compute correlation
	  for(int top_channel = 0; top_channel < SIZE_1(top); top_channel++) {
	    sum[ch_off] = 0;
	  
	    int s2o = (top_channel % 9) - 4;
	    int s2p = (top_channel / 9) - 4;
	    
	    for (int j = 0; j < 1; j++) { // HEIGHT
	      for (int i = 0; i < 1; i++) { // WIDTH
	        int ji_off = ((j * 1) + i) * SIZE_3(rbot0);
	        for (int ch = ch_off; ch < SIZE_3(rbot0); ch += 32) { // CHANNELS
	          int x2 = x1 + s2o;
	          int y2 = y1 + s2p;
	          
	          int idxPatchData = ji_off + ch;
	          int idx2 = ((item * SIZE_1(rbot0) + y2+j) * SIZE_2(rbot0) + x2+i) * SIZE_3(rbot0) + ch;
	          
	          sum[ch_off] += patch_data[idxPatchData] * rbot1[idx2];
	        }
	      }
	    }
	    
	    __syncthreads();
	    
	    if (ch_off == 0) {
	      float total_sum = 0;
	      for (int idx = 0; idx < 32; idx++) {
	        total_sum += sum[idx];
	      }
	      const int sumelems = SIZE_3(rbot0);
	      const int index = ((top_channel*SIZE_2(top) + blockIdx.y)*SIZE_3(top))+blockIdx.x;
	      top[index + item*SIZE_1(top)*SIZE_2(top)*SIZE_3(top)] = total_sum / (float)sumelems;
	    }
	  } 
	}
'''

def cupy_kernel(strFunction, objectVariables):
	strKernel = globals()[strFunction]

	while True:
		objectMatch = re.search('(SIZE_)([0-4])(\()([^\)]*)(\))', strKernel)

		if objectMatch is None:
			break
		# end

		intArg = int(objectMatch.group(2))

		strTensor = objectMatch.group(4)

		intSizes = objectVariables[strTensor]
		# print("intSizes ",intSizes)

		strKernel = strKernel.replace(objectMatch.group(), str(int(intSizes[intArg])))
	# end

	while True:
		objectMatch = re.search('(VALUE_)([0-4])(\()([^\)]+)(\))', strKernel)

		if objectMatch is None:
			break
		# end

		intArgs = int(objectMatch.group(2))
		strArgs = objectMatch.group(4).split(',')

		strTensor = strArgs[0]
		intStrides = objectVariables[strTensor].stride()
		strIndex = [ '((' + strArgs[intArg + 1].replace('{', '(').replace('}', ')').strip() + ')*' + str(intStrides[intArg]) + ')' for intArg in range(intArgs) ]

		strKernel = strKernel.replace(objectMatch.group(0), strTensor + '[' + str.join('+', strIndex) + ']')
	# end

	return strKernel
# end

@cupy.util.memoize(for_each_device=True)
def cupy_launch(strFunction, strKernel):
	return cupy.cuda.compile_with_cache(strKernel).get_function(strFunction)
# end

class FunctionCorrelation(torch.nn.Module):
	def __init__(self):
		super(FunctionCorrelation, self).__init__()
	# end

	def forward(self, first, second, intL):
		# self.save_for_backward(first, second)
		# first = torch.ones(first_.size(0), first_.size(2) + 8, first_.size(3) + 8, first_.size(1),dtype=torch.float64, device='cuda:0')
		assert(first.is_contiguous() == True)
		assert(second.is_contiguous() == True)

		list_size = [1, 32, 96, 160,1, 64, 48, 80,1, 96, 24, 40,1, 128, 12, 20,1, 196, 6, 10]
		size_0 = list_size[(intL-2)*4]
		size_1 = list_size[(intL-2)*4+1]
		size_2 = list_size[(intL-2)*4+2]
		size_3 = list_size[(intL-2)*4+3]
		
		# self.rbot0 = first.new(first.size(0), first.size(2) + 8, first.size(3) + 8, first.size(1)).zero_()
		# self.rbot1 = first.new(first.size(0), first.size(2) + 8, first.size(3) + 8, first.size(1)).zero_()device=cuda0

		self.rbot0 = torch.zeros([size_0,size_2 + 8, size_3 + 8,size_1],device='cuda:0')	
		self.rbot1 = torch.zeros([size_0,size_2 + 8, size_3 + 8,size_1],device='cuda:0')		

		# output = first.new(first.size(0), 81, first.size(2), first.size(3)).zero_()
		output = torch.zeros([size_0,81, size_2,size_3],device='cuda:0')

		if first.is_cuda == True:
			class Stream:
				ptr = torch.cuda.current_stream().cuda_stream
			# end

			n = size_2 * size_3
			strkern = cupy_kernel('kernel_Correlation_rearrange', {
				'input': [size_0, size_1, size_2, size_3],
				'output': [size_0,size_2 + 8, size_3 + 8,size_1]
			})
			cupy_launch('kernel_Correlation_rearrange',strkern )(
				grid=tuple([ int((n + 16 - 1) // 16), size_1, size_0 ]),
				block=tuple([ 16, 1, 1 ]),
				args=[ n, first.data_ptr(), self.rbot0.data_ptr() ],
				stream=Stream
			)

			n = size_2 * size_3
			cupy_launch('kernel_Correlation_rearrange', cupy_kernel('kernel_Correlation_rearrange', {
				'input': [size_0, size_1, size_2, size_3],
				'output': [size_0,size_2 + 8, size_3 + 8,size_1]
			}))(
				grid=tuple([ int((n + 16 - 1) // 16), size_1, size_0 ]),
				block=tuple([ 16, 1, 1 ]),
				args=[ int(n), second.data_ptr(), self.rbot1.data_ptr() ],
				stream=Stream
			)

			n = size_1 * size_2 * size_3
			cupy_launch('kernel_Correlation_updateOutput', cupy_kernel('kernel_Correlation_updateOutput', {
				'rbot0': [size_0,size_2 + 8, size_3 + 8,size_1],
				'rbot1': [size_0,size_2 + 8, size_3 + 8,size_1],
				'top': [size_0,81, size_2,size_3]
			}))(
				grid=tuple([ size_3, size_2, size_0 ]),
				block=tuple([ 32, 1, 1 ]),
				shared_mem=size_1 * 4,
				args=[ int(n), self.rbot0.data_ptr(), self.rbot1.data_ptr(), output.data_ptr() ],
				stream=Stream
			)

		elif first.is_cuda == False:
			raise NotImplementedError()

		# end

		return output
	# end

	# def backward(self, gradOutput):
	# 	# first, second = self.saved_tensors

	# 	assert(gradOutput.is_contiguous() == True)

	# 	gradFirst = first.new(first.size()).zero_() if self.needs_input_grad[0] == True else None
	# 	gradSecond = first.new(first.size()).zero_() if self.needs_input_grad[1] == True else None

	# 	if first.is_cuda == True:
	# 		raise NotImplementedError()

	# 	elif first.is_cuda == False:
	# 		raise NotImplementedError()

	# 	# end

	# 	return gradFirst, gradSecond
	# end
# end

class ModuleCorrelation(torch.nn.Module):
	def __init__(self):
		super(ModuleCorrelation, self).__init__()
	# end

	def forward(self, tensorFirst, tensorSecond,intL):
		return FunctionCorrelation()(tensorFirst, tensorSecond,intL)
	# end
# end